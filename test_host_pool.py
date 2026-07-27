"""HostPool tests: offline, no network, no DB. See implementation.md "Tests".

Pacing is asserted against the event loop's own clock rather than wall time, so the
timing tests are exact and instant. `asyncio.sleep` is patched to advance a fake clock;
`loop.call_later` is patched onto the same clock so cooldowns are deterministic too.
"""

import asyncio
import unittest
from unittest.mock import patch

from host_pool import COOLDOWN_CAP, Host, HostPool, Outcome

URLS = ["https://a.test", "https://b.test", "https://c.test"]

# Captured before any patching: the harness must be able to yield to the event loop
# without re-entering the fake clock's sleep.
_REAL_SLEEP = asyncio.sleep


class FakeClock:
    """Drives asyncio.sleep and loop.call_later off a virtual monotonic clock.

    Sleepers are released in deadline order, so N coroutines paced by HostPool
    interleave exactly as they would in real time, with no real waiting.
    """

    def __init__(self):
        self.now = 1000.0
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._timers: list[tuple[float, int, callable, tuple]] = []
        self._seq = 0

    def time(self) -> float:
        return self.now

    async def sleep(self, delay, result=None):
        if delay <= 0:
            await _REAL_SLEEP(0)
            return result
        self._seq += 1
        fut = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, self._seq, fut))
        await fut
        return result

    def call_later(self, delay, callback, *args):
        self._seq += 1
        entry = (self.now + max(delay, 0.0), self._seq, callback, args)
        self._timers.append(entry)
        return _FakeTimer(self, entry)

    def cancel_timer(self, entry):
        if entry in self._timers:
            self._timers.remove(entry)

    async def advance_to_next(self) -> bool:
        """Jump to the earliest pending deadline and fire everything due there."""
        await _drain()
        deadlines = [d for d, _, _ in self._sleepers] + [d for d, _, _, _ in self._timers]
        if not deadlines:
            return False
        self.now = max(self.now, min(deadlines))

        due_timers = [t for t in self._timers if t[0] <= self.now]
        for entry in sorted(due_timers, key=lambda t: (t[0], t[1])):
            self._timers.remove(entry)
            entry[2](*entry[3])

        due_sleepers = [s for s in self._sleepers if s[0] <= self.now]
        for entry in sorted(due_sleepers, key=lambda s: (s[0], s[1])):
            self._sleepers.remove(entry)
            if not entry[2].done():
                entry[2].set_result(None)
        await _drain()
        return True

    async def run_until_idle(self, limit: int = 10_000) -> None:
        for _ in range(limit):
            if not await self.advance_to_next():
                return
        raise AssertionError("fake clock did not settle")


class _FakeTimer:
    def __init__(self, clock, entry):
        self._clock = clock
        self._entry = entry

    def cancel(self):
        self._clock.cancel_timer(self._entry)


async def _yield():
    await _REAL_SLEEP(0)


async def _drain():
    for _ in range(50):
        await _REAL_SLEEP(0)


class PoolHarness:
    """HostPool wired to a fake clock and a scripted `_request`."""

    def __init__(self, urls=URLS, rate=1.0, block_cooldown=60.0, responses=None):
        self.clock = FakeClock()
        self.pool = HostPool(
            session=None,
            base_urls=urls,
            requests_per_second_per_domain=rate,
            block_cooldown=block_cooldown,
            request_timeout=15.0,
        )
        self.calls: list[tuple[str, float]] = []
        self.responses = responses or {}
        self.inflight = 0
        self.max_inflight = 0
        self.inflight_by_host: dict[str, int] = {}
        self.max_inflight_by_host = 0
        self.pool._request = self._request

    async def _request(self, host: Host, path: str, validate, is_blocked=None):
        self.calls.append((host.base_url, self.clock.now))
        self.inflight += 1
        self.inflight_by_host[host.base_url] = self.inflight_by_host.get(host.base_url, 0) + 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.max_inflight_by_host = max(
            self.max_inflight_by_host, self.inflight_by_host[host.base_url]
        )
        try:
            await _yield()  # simulate a round trip that yields to the loop
            responder = self.responses.get(host.base_url, self.responses.get("*"))
            if callable(responder):
                return responder(host, path)
            return responder or Outcome(kind="ok", status=200, base_url=host.base_url)
        finally:
            self.inflight -= 1
            self.inflight_by_host[host.base_url] -= 1

    def patched(self):
        # Patched on host_pool's own namespace, not on the asyncio module, so the
        # harness and asyncio internals keep the real implementations.
        return patch.multiple(
            "host_pool",
            sleep=self.clock.sleep,
            get_running_loop=lambda: _FakeLoop(self.clock),
        )


class _FakeLoop:
    def __init__(self, clock):
        self._clock = clock

    def time(self):
        return self._clock.time()

    def call_later(self, delay, callback, *args):
        return self._clock.call_later(delay, callback, *args)


def run(coro):
    return asyncio.run(coro)


class TestPacing(unittest.TestCase):
    def test_fractional_rate_returns(self):
        """The regression for the old token bucket, which hung forever below 1 rps."""

        async def scenario():
            h = PoolHarness(urls=["https://a.test"], rate=0.5)
            with h.patched():
                task = asyncio.ensure_future(h.pool.fetch("/x"))
                await h.clock.run_until_idle()
                return await asyncio.wait_for(task, timeout=1)

        outcome = run(scenario())
        self.assertEqual(outcome.kind, "ok")

    def test_ten_requests_at_half_rate_span_18s(self):
        async def scenario():
            h = PoolHarness(urls=["https://a.test"], rate=0.5)
            with h.patched():
                tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(10)]
                await h.clock.run_until_idle()
                await asyncio.gather(*tasks)
            return h.calls

        calls = run(scenario())
        self.assertEqual(len(calls), 10)
        self.assertAlmostEqual(calls[-1][1] - calls[0][1], 18.0, places=6)

    def test_ten_requests_at_full_rate_span_9s(self):
        async def scenario():
            h = PoolHarness(urls=["https://a.test"], rate=1.0)
            with h.patched():
                tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(10)]
                await h.clock.run_until_idle()
                await asyncio.gather(*tasks)
            return h.calls

        calls = run(scenario())
        self.assertAlmostEqual(calls[-1][1] - calls[0][1], 9.0, places=6)


class TestDistribution(unittest.TestCase):
    def test_nine_fetches_across_three_hosts_is_three_each(self):
        async def scenario():
            h = PoolHarness()
            with h.patched():
                tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(9)]
                await h.clock.run_until_idle()
                await asyncio.gather(*tasks)
            return h.calls

        calls = run(scenario())
        counts = {url: sum(1 for c in calls if c[0] == url) for url in URLS}
        self.assertEqual(counts, {url: 3 for url in URLS})

    def test_concurrency_is_bounded_by_host_count(self):
        async def scenario():
            h = PoolHarness()
            with h.patched():
                tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(30)]
                await h.clock.run_until_idle()
                await asyncio.gather(*tasks)
            return h.max_inflight, h.max_inflight_by_host

        max_total, max_per_host = run(scenario())
        self.assertEqual(max_per_host, 1)
        self.assertLessEqual(max_total, len(URLS))


class TestCooling(unittest.TestCase):
    def test_blocked_host_is_withheld_for_the_cooldown(self):
        """The other hosts absorb the load meanwhile, so enough work is queued here to
        run past the 60s cooldown and observe the blocked host return."""

        async def scenario():
            blocked = Outcome(kind="blocked", status=429, base_url="https://a.test")
            h = PoolHarness(
                responses={"https://a.test": blocked, "*": Outcome(kind="ok", status=200)},
                block_cooldown=60.0,
            )
            with h.patched():
                tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(140)]
                await h.clock.run_until_idle()
                await asyncio.gather(*tasks)
            return h.calls

        calls = run(scenario())
        a_calls = [t for url, t in calls if url == "https://a.test"]
        self.assertGreaterEqual(len(a_calls), 2)
        self.assertGreaterEqual(a_calls[1] - a_calls[0], 60.0)
        # Nothing was routed to the cooling host inside its window.
        window = [t for t in a_calls if a_calls[0] < t < a_calls[0] + 60.0]
        self.assertEqual(window, [])

    def test_404_and_parse_valid_page_do_not_cool(self):
        for kind, status in (("not_found", 404), ("ok", 200)):
            with self.subTest(kind=kind):

                async def scenario(kind=kind, status=status):
                    h = PoolHarness(
                        urls=["https://a.test"],
                        rate=1.0,
                        responses={"*": Outcome(kind=kind, status=status)},
                    )
                    with h.patched():
                        tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(3)]
                        await h.clock.run_until_idle()
                        await asyncio.gather(*tasks)
                    return h.calls

                calls = run(scenario())
                # Paced at 1/s only -- no cooldown added on top.
                self.assertAlmostEqual(calls[-1][1] - calls[0][1], 2.0, places=6)

    def test_timeout_and_connection_error_cool_briefly(self):
        for kind in ("timeout", "connection_error", "server_error"):
            with self.subTest(kind=kind):

                async def scenario(kind=kind):
                    h = PoolHarness(
                        urls=["https://a.test"], rate=1.0, responses={"*": Outcome(kind=kind)}
                    )
                    with h.patched():
                        tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(2)]
                        await h.clock.run_until_idle()
                        await asyncio.gather(*tasks)
                    return h.calls

                calls = run(scenario())
                # BRIEF_COOLDOWN (5s) dominates the 1s pacing interval.
                self.assertAlmostEqual(calls[1][1] - calls[0][1], 5.0, places=6)

    def test_all_hosts_cooling_makes_callers_wait_without_spinning(self):
        async def scenario():
            h = PoolHarness(responses={"*": Outcome(kind="blocked", status=403)})
            with h.patched():
                tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(3)]
                await _drain()
                # All three blocked and cooling: a fourth caller must not get a host.
                fourth = asyncio.ensure_future(h.pool.fetch("/x"))
                await _drain()
                pending_before = not fourth.done()
                calls_before = len(h.calls)
                await h.clock.run_until_idle()
                await asyncio.gather(*tasks, fourth)
            return pending_before, calls_before

        pending_before, calls_before = run(scenario())
        self.assertTrue(pending_before)
        self.assertEqual(calls_before, 3)

    def test_consecutive_blocks_double_and_success_resets(self):
        host = Host("https://a.test", block_cooldown_base=60.0)
        pool = HostPool.__new__(HostPool)

        first = pool._cooldown_for(host, Outcome(kind="blocked", status=429))
        second = pool._cooldown_for(host, Outcome(kind="blocked", status=429))
        third = pool._cooldown_for(host, Outcome(kind="blocked", status=429))
        self.assertEqual([first, second, third], [60.0, 120.0, 240.0])

        self.assertEqual(pool._cooldown_for(host, Outcome(kind="ok", status=200)), 0.0)
        self.assertEqual(pool._cooldown_for(host, Outcome(kind="blocked", status=429)), 60.0)

    def test_block_cooldown_is_capped(self):
        host = Host("https://a.test", block_cooldown_base=60.0)
        pool = HostPool.__new__(HostPool)
        for _ in range(20):
            cooldown = pool._cooldown_for(host, Outcome(kind="blocked", status=429))
        self.assertEqual(cooldown, COOLDOWN_CAP)


class TestRetryAfter(unittest.TestCase):
    def setUp(self):
        self.host = Host("https://a.test", block_cooldown_base=60.0)
        self.pool = HostPool.__new__(HostPool)

    def _cooldown(self, retry_after):
        return self.pool._cooldown_for(
            self.host, Outcome(kind="blocked", status=429, retry_after=retry_after)
        )

    def test_absurd_value_is_capped(self):
        self.assertEqual(self._cooldown(99999.0), COOLDOWN_CAP)

    def test_past_date_negative_and_zero_fall_back_to_configured_cooldown(self):
        for value in (-30.0, 0.0):
            with self.subTest(value=value):
                self.host.current_block_cooldown = 60.0
                self.assertEqual(self._cooldown(value), 60.0)

    def test_absent_falls_back_to_configured_cooldown(self):
        self.assertEqual(self._cooldown(None), 60.0)

    def test_honoured_when_reasonable(self):
        self.assertEqual(self._cooldown(30.0), 30.0)


class TestCancellation(unittest.TestCase):
    def test_cancelled_fetch_returns_its_host(self):
        async def scenario():
            h = PoolHarness(urls=["https://a.test"], rate=1.0)
            with h.patched():
                for _ in range(3):
                    task = asyncio.ensure_future(h.pool.fetch("/x"))
                    await _drain()
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    await _drain()
                # The pool must still be usable after repeated cancellation.
                task = asyncio.ensure_future(h.pool.fetch("/x"))
                await h.clock.run_until_idle()
                return await asyncio.wait_for(task, timeout=1)

        outcome = run(scenario())
        self.assertEqual(outcome.kind, "ok")


class TestExhaustionLogging(unittest.TestCase):
    def test_all_cooling_logs_once_per_episode_not_once_per_caller(self):
        """One block per host, then 200 callers queued behind the cooldown.

        Logging on Queue.empty() would emit a line for every one of those callers;
        logging on the transition emits exactly one.
        """

        blocked_once: dict[str, bool] = {}

        def responder(host, path):
            if not blocked_once.get(host.base_url):
                blocked_once[host.base_url] = True
                return Outcome(kind="blocked", status=403, base_url=host.base_url)
            return Outcome(kind="ok", status=200, base_url=host.base_url)

        async def scenario():
            h = PoolHarness(responses={"*": responder})
            with h.patched():
                with self.assertLogs("host_pool", level="ERROR") as captured:
                    tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(200)]
                    await h.clock.run_until_idle()
                    await asyncio.gather(*tasks)
            return captured.output

        output = run(scenario())
        all_cooling = [line for line in output if "all 3 host(s) cooling" in line]
        self.assertEqual(len(all_cooling), 1)

    def test_repeated_full_outage_logs_once_per_episode(self):
        """Sanity bound on the pathological case: every request blocked. Still one line
        per all-cooling episode, never one per caller."""

        async def scenario():
            h = PoolHarness(responses={"*": Outcome(kind="blocked", status=403)})
            with h.patched():
                with self.assertLogs("host_pool", level="ERROR") as captured:
                    tasks = [asyncio.ensure_future(h.pool.fetch("/x")) for _ in range(200)]
                    await h.clock.run_until_idle()
                    await asyncio.gather(*tasks)
            return captured.output

        output = run(scenario())
        all_cooling = [line for line in output if "all 3 host(s) cooling" in line]
        # ~one episode per round of three blocked requests, not one per caller.
        self.assertLessEqual(len(all_cooling), 200 // len(URLS) + 2)


class TestRequestClassification(unittest.IsolatedAsyncioTestCase):
    """_request must convert every expected transport failure into an Outcome, or
    fetch()'s finally would return the host with no cooldown."""

    async def _classify(self, fetch_page_result=None, exc=None, validate=None, is_blocked=None):
        pool = HostPool(
            session=object(),
            base_urls=["https://a.test"],
            requests_per_second_per_domain=1.0,
            block_cooldown=60.0,
            request_timeout=15.0,
        )

        async def fake_fetch_page(session, url, timeout):
            if exc is not None:
                raise exc
            return fetch_page_result

        with patch("host_pool.fetch_page", fake_fetch_page):
            return await pool._request(pool._hosts[0], "/x", validate, is_blocked)

    async def test_timeout_becomes_outcome(self):
        outcome = await self._classify(exc=asyncio.TimeoutError())
        self.assertEqual(outcome.kind, "timeout")

    async def test_connection_error_becomes_outcome(self):
        import aiohttp

        outcome = await self._classify(exc=aiohttp.ClientConnectionError("boom"))
        self.assertEqual(outcome.kind, "connection_error")

    async def test_status_mapping(self):
        cases = {
            404: "not_found",
            403: "blocked",
            429: "blocked",
            500: "server_error",
            503: "server_error",
            418: "unexpected_status",
            200: "ok",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                outcome = await self._classify((status, "<html></html>", None, None))
                self.assertEqual(outcome.kind, expected)

    async def test_redirect_is_not_followed_and_keeps_location(self):
        outcome = await self._classify((302, None, "/elsewhere", None))
        self.assertEqual(outcome.kind, "redirect")
        self.assertEqual(outcome.http_status, "302->/elsewhere")

    async def test_unrecognized_200_is_not_a_block(self):
        """The soft-404 case. Cooling here would stall the frontier, since every ID
        above the true maximum returns a 200 that is not a details page."""
        outcome = await self._classify(
            (200, "nope", None, None), validate=lambda html: False
        )
        self.assertEqual(outcome.kind, "unrecognized")
        pool = HostPool.__new__(HostPool)
        host = Host("https://a.test", block_cooldown_base=60.0)
        self.assertEqual(pool._cooldown_for(host, outcome), 0.0)

    async def test_positively_identified_challenge_is_a_block(self):
        outcome = await self._classify(
            (200, "<html>_cf_chl_opt</html>", None, None),
            validate=lambda html: False,
            is_blocked=lambda html: "_cf_chl_opt" in html,
        )
        self.assertEqual(outcome.kind, "challenge")
        pool = HostPool.__new__(HostPool)
        host = Host("https://a.test", block_cooldown_base=60.0)
        self.assertEqual(pool._cooldown_for(host, outcome), 60.0)


if __name__ == "__main__":
    unittest.main()
