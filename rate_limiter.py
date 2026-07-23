"""Token bucket + concurrency semaphore + circuit breaker. See PLAN.md Non-negotiable 3.

Concurrency and requests/sec are independent knobs. Starts conservative, ramps up on
a sustained clean window, trips a circuit breaker on 403/429/5xx/timeout/conn-error/
challenge markers/latency spikes/parse_error spikes. Plain 404 is not a failure signal.
"""

import asyncio
import logging
import time

log = logging.getLogger("rate_limiter")

FAILURE_KINDS = {"blocked", "server_error", "timeout", "connection_error", "challenge", "latency_spike", "parse_error"}


class AdaptiveLimiter:
    def __init__(
        self,
        min_concurrency: int = 2,
        max_concurrency: int = 8,
        min_rate: float = 1.0,
        max_rate: float = 4.0,
        clean_window_requests: int = 50,
        breaker_cooldown_base: float = 30.0,
    ):
        self.min_concurrency = min_concurrency
        self.max_concurrency = max_concurrency
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.clean_window_requests = clean_window_requests
        self.breaker_cooldown_base = breaker_cooldown_base

        self._concurrency = min_concurrency
        self._rate = min_rate
        self._inflight = 0
        self._cond = asyncio.Condition()

        self._tokens = min_rate
        self._last_refill = time.monotonic()

        self._tripped_until = 0.0
        self._trip_count = 0
        self._clean_streak = 0

    async def acquire(self) -> None:
        async with self._cond:
            await self._cond.wait_for(self._slot_available)
            self._inflight += 1
        await self._consume_token()

    async def release(self) -> None:
        async with self._cond:
            self._inflight -= 1
            self._cond.notify_all()

    def _slot_available(self) -> bool:
        return self._inflight < self._concurrency and time.monotonic() >= self._tripped_until

    async def _consume_token(self) -> None:
        while True:
            now = time.monotonic()
            if now < self._tripped_until:
                await asyncio.sleep(self._tripped_until - now)
                continue
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep(max((1 - self._tokens) / self._rate, 0.01))

    def note_success(self) -> None:
        self._clean_streak += 1
        if self._clean_streak >= self.clean_window_requests:
            self._clean_streak = 0
            self._ramp_up()

    def note_failure(self, kind: str, retry_after: float | None = None) -> None:
        self._clean_streak = 0
        if kind not in FAILURE_KINDS:
            return
        self._trip(retry_after)

    def _ramp_up(self) -> None:
        old_c, old_r = self._concurrency, self._rate
        self._concurrency = min(self._concurrency + 1, self.max_concurrency)
        self._rate = min(round(self._rate + 1, 2), self.max_rate)
        if (self._concurrency, self._rate) != (old_c, old_r):
            log.info(
                "ramp up: concurrency %d->%d rate %.2f->%.2f",
                old_c, self._concurrency, old_r, self._rate,
            )

    def _trip(self, retry_after: float | None) -> None:
        self._trip_count += 1
        self._concurrency = self.min_concurrency
        self._rate = self.min_rate
        cooldown = retry_after if retry_after is not None else self.breaker_cooldown_base * (2 ** (self._trip_count - 1))
        cooldown = min(cooldown, 3600.0)
        self._tripped_until = max(self._tripped_until, time.monotonic() + cooldown)
        log.warning(
            "circuit breaker tripped (count=%d), cooldown=%.1fs, concurrency/rate reset to floor",
            self._trip_count, cooldown,
        )

    def slot(self):
        return _LimiterSlot(self)


class _LimiterSlot:
    def __init__(self, limiter: AdaptiveLimiter):
        self._limiter = limiter

    async def __aenter__(self):
        await self._limiter.acquire()
        return self._limiter

    async def __aexit__(self, exc_type, exc, tb):
        await self._limiter.release()
        return False
