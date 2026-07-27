"""Fixed-rate host queue. See implementation.md "Rate limiting and host pool".

Each host sits in an asyncio.Queue exactly once. That single fact provides, with no
locks and no selection logic:

- atomic host selection (Queue.get() is atomic),
- at most one in-flight request per domain, so total concurrency is len(hosts),
- fair distribution, since hosts are re-enqueued as they finish,
- a cooling host that is genuinely absent from circulation rather than skipped over,
- callers that simply wait when every host is cooling.

Pacing is one monotonic deadline per host, so fractional rates need no special
handling: the interval is 1/rate, which is 2.0s at 0.5 req/s.
"""

import asyncio
import logging
from asyncio import get_running_loop, sleep
from dataclasses import dataclass, field

import aiohttp

from scraper import fetch_page

log = logging.getLogger("host_pool")

COOLDOWN_CAP = 900.0  # 15 min; also the Retry-After cap
BRIEF_COOLDOWN = 5.0  # 5xx / timeout / connection error

# A block means the host is refusing us: cool it, and escalate on repeats.
BLOCK_KINDS = frozenset({"blocked", "challenge"})
# Overload or transport trouble: back off briefly, but do not escalate.
BRIEF_COOL_KINDS = frozenset({"server_error", "timeout", "connection_error"})


@dataclass(slots=True)
class Outcome:
    """Every expected HTTP and transport result, as a value rather than an exception.

    `_request()` never raises for these — if it did, `fetch()`'s finally block would
    return the host with no cooldown and the 5xx/timeout backoff in implementation.md
    would silently not apply, in exactly the overload case that needs it most.
    """

    kind: str
    status: int | None = None
    html: str | None = None
    location: str | None = None
    retry_after: float | None = None
    base_url: str | None = None

    @property
    def http_status(self) -> str | None:
        if self.kind == "redirect":
            return f"{self.status}->{self.location}"
        return str(self.status) if self.status is not None else None


@dataclass(slots=True)
class Host:
    base_url: str
    block_cooldown_base: float
    next_start_at: float = 0.0
    current_block_cooldown: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.current_block_cooldown = self.block_cooldown_base


class HostPool:
    """One fetch() entry point. There is deliberately no public acquire()/release()
    pair, so a caller cannot leak a host slot."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_urls: list[str],
        requests_per_second_per_domain: float,
        block_cooldown: float,
        request_timeout: float,
    ):
        if not base_urls:
            raise ValueError("HostPool needs at least one base URL")
        self._session = session
        self._interval = 1.0 / requests_per_second_per_domain
        self._request_timeout = request_timeout
        self._hosts = [Host(url, block_cooldown) for url in base_urls]
        self._queue: asyncio.Queue[Host] = asyncio.Queue()
        for host in self._hosts:
            self._queue.put_nowait(host)
        # A host is in exactly one place at a time: the queue, in flight, or a pending
        # cooldown timer. So at most one timer per host, keyed by URL.
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._cooling = 0
        self._all_cooling_logged = False

    @property
    def host_count(self) -> int:
        return len(self._hosts)

    async def fetch(self, path: str, validate=None, is_blocked=None) -> Outcome:
        """Fetch `path` from the next available host.

        `validate` recognizes the page we wanted; `is_blocked` positively recognizes a
        block/interstitial. A 200 that is neither is `unrecognized` and does NOT cool
        the host — on this site most of those are soft-404s for IDs that do not exist
        yet, and cooling on them stalls the frontier (see implementation.md "Rate limiting and host pool").

        Both predicates run here, inside the fetch, because by the time the caller sees
        the Outcome the host has already been returned to the queue.
        """
        host = await self._queue.get()
        cooldown = 0.0
        try:
            loop = get_running_loop()
            delay = host.next_start_at - loop.time()
            if delay > 0:
                await sleep(delay)
            host.next_start_at = loop.time() + self._interval

            outcome = await self._request(host, path, validate, is_blocked)
            cooldown = self._cooldown_for(host, outcome)
            return outcome
        finally:
            # Runs on cancellation too: this is the whole cancellation story, and it
            # replaces the in-flight-counter rollback the old limiter needed.
            self._return_later(host, cooldown)

    async def _request(self, host: Host, path: str, validate, is_blocked=None) -> Outcome:
        url = f"{host.base_url}{path}"
        try:
            status, html, location, retry_after = await fetch_page(
                self._session, url, self._request_timeout
            )
        except asyncio.TimeoutError:
            return Outcome(kind="timeout", base_url=host.base_url)
        except aiohttp.ClientConnectionError:
            return Outcome(kind="connection_error", base_url=host.base_url)
        except aiohttp.ClientError as exc:
            log.warning("client error fetching %s: %s", url, exc)
            return Outcome(kind="connection_error", base_url=host.base_url)

        common = {"status": status, "retry_after": retry_after, "base_url": host.base_url}

        if status == 404:
            return Outcome(kind="not_found", **common)
        if status in (301, 302, 303, 307, 308):
            return Outcome(kind="redirect", location=location, **common)
        if status in (403, 429):
            return Outcome(kind="blocked", **common)
        if status >= 500:
            return Outcome(kind="server_error", **common)
        if status != 200:
            # 400/410/418 and friends: neither a block signal nor something we can
            # classify. Record it, do not cool the host.
            return Outcome(kind="unexpected_status", **common)
        if validate is None or validate(html):
            return Outcome(kind="ok", html=html, **common)
        if is_blocked is not None and is_blocked(html):
            return Outcome(kind="challenge", html=html, **common)
        # A 200 we do not recognize. Deliberately NOT a cooling signal: guessing that
        # an unfamiliar page is a block is what escalated the old breaker to its cap.
        return Outcome(kind="unrecognized", html=html, **common)

    def _cooldown_for(self, host: Host, outcome: Outcome) -> float:
        if outcome.kind in BLOCK_KINDS:
            cooldown = self._normalize_retry_after(outcome.retry_after, host)
            # Consecutive blocks double; any non-block response resets (below).
            host.current_block_cooldown = min(host.current_block_cooldown * 2, COOLDOWN_CAP)
            return cooldown

        host.current_block_cooldown = host.block_cooldown_base
        if outcome.kind in BRIEF_COOL_KINDS:
            return BRIEF_COOLDOWN
        return 0.0

    @staticmethod
    def _normalize_retry_after(retry_after: float | None, host: Host) -> float:
        """Absent, malformed, non-finite or non-positive -> configured cooldown. Then cap.

        A negative value is reachable via the HTTP-date form (past date or clock skew)
        and must not schedule a host's return in the past; an absurd one must not park
        a host for the rest of the day.
        """
        if retry_after is None or retry_after <= 0:
            return min(host.current_block_cooldown, COOLDOWN_CAP)
        return min(retry_after, COOLDOWN_CAP)

    def _return_later(self, host: Host, cooldown: float) -> None:
        if cooldown <= 0:
            self._queue.put_nowait(host)
            return

        self._cooling += 1
        if self._cooling == len(self._hosts) and not self._all_cooling_logged:
            # Logged on the transition only. An empty queue is the normal state
            # whenever every host is busy, so logging on Queue.empty() would bury this
            # under one line per waiting caller in a 500-ID batch.
            self._all_cooling_logged = True
            log.error(
                "all %d host(s) cooling; crawl paused until the earliest recovers",
                len(self._hosts),
            )
        log.warning("cooling %s for %.1fs", host.base_url, cooldown)
        loop = get_running_loop()
        self._timers[host.base_url] = loop.call_later(cooldown, self._wake, host)

    def _wake(self, host: Host) -> None:
        self._timers.pop(host.base_url, None)
        was_all_cooling = self._cooling == len(self._hosts)
        self._cooling -= 1
        if was_all_cooling:
            self._all_cooling_logged = False
            log.info("%s back in rotation; crawl resuming", host.base_url)
        self._queue.put_nowait(host)

    def close(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
