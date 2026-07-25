"""Unified crawler loop: frontier scan/extend -> backward drain chunk -> age-tiered refresh.
See PLAN.md "Unified crawler loop".
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

import db
from rate_limiter import AdaptiveLimiter
from scraper import ParseError, fetch_details, looks_like_details_page, parse_details_html

log = logging.getLogger("crawler")

REFRESH_TIERS = [
    (timedelta(days=7), timedelta(hours=6)),
    (timedelta(days=30), timedelta(days=1)),
    (None, timedelta(days=7)),
]


def next_refresh_delay(added_at: datetime | None) -> timedelta:
    if added_at is None:
        return REFRESH_TIERS[-1][1]
    age = datetime.now(timezone.utc) - added_at
    for max_age, cadence in REFRESH_TIERS:
        if max_age is None or age < max_age:
            return cadence
    return REFRESH_TIERS[-1][1]


class Crawler:
    def __init__(self, config: dict, pool, session: aiohttp.ClientSession, limiter: AdaptiveLimiter):
        self.config = config
        self.pool = pool
        self.session = session
        self.limiter = limiter
        self.base_url = config["BASE_URL"]
        self.tz_name = config["SITE_TZ"]

    async def fetch_and_classify(self, torrent_id: int):
        """Fetch one details page, run it through the rate limiter, classify the outcome.
        Returns (kind, data_or_none, http_status_str).
        kind in: 'success', 'not_found', 'redirect', 'parse_error', 'blocked', 'server_error',
                 'timeout', 'connection_error'.
        """
        async with self.limiter.slot() as limiter:
            try:
                status, html, location, retry_after = await fetch_details(self.session, self.base_url, torrent_id)
            except asyncio.TimeoutError:
                limiter.note_failure("timeout")
                return "timeout", None, None
            except aiohttp.ClientConnectionError:
                limiter.note_failure("connection_error")
                return "connection_error", None, None
            except aiohttp.ClientError as exc:
                limiter.note_failure("connection_error")
                log.warning("client error fetching %s: %s", torrent_id, exc)
                return "connection_error", None, None

            if status == 404:
                limiter.note_success()
                return "not_found", None, "404"

            if status in (301, 302, 303, 307, 308):
                limiter.note_success()
                return "redirect", None, f"{status}->{location}"

            if status in (403, 429):
                limiter.note_failure("blocked", retry_after=retry_after)
                return "blocked", None, str(status)

            if status >= 500:
                limiter.note_failure("server_error")
                return "server_error", None, str(status)

            if status != 200:
                # Unexpected status (e.g. 400/410/418): not a 404/redirect we can
                # classify, but not a block/overload signal either. Feed it into
                # neither the clean-streak (would wrongly push ramp-up) nor the
                # breaker (would wrongly trip); just record it as transient.
                return "parse_error", None, str(status)

            if not looks_like_details_page(html):
                limiter.note_failure("challenge")
                return "parse_error", None, "200-no-structure"

            try:
                data = parse_details_html(html, torrent_id, self.tz_name)
            except ParseError as exc:
                limiter.note_failure("parse_error")
                log.warning("parse_error on %s: %s", torrent_id, exc)
                return "parse_error", None, "200-parse-error"

            limiter.note_success()
            return "success", data, "200"

    async def resolve_id(self, torrent_id: int, gap_relative_to_highest: bool):
        kind, data, http_status = await self.fetch_and_classify(torrent_id)
        now = datetime.now(timezone.utc)

        if kind == "success":
            data["next_refresh_at"] = now + next_refresh_delay(data["added_at"])
            await db.upsert_torrent(self.pool, data)
            return kind, data

        if kind == "not_found":
            status = "internal_gap" if gap_relative_to_highest else "frontier_missing"
            next_retry = now + timedelta(minutes=5) if status == "frontier_missing" else now + timedelta(days=30)
            await db.upsert_id_status(self.pool, torrent_id, status, http_status, next_retry)
            return kind, None

        if kind == "redirect":
            await db.upsert_id_status(self.pool, torrent_id, "gone", http_status, None)
            return kind, None

        # parse_error / blocked / server_error / timeout / connection_error -> transient, retry soon
        status = "parse_error" if kind == "parse_error" else "transient_error"
        next_retry = now + timedelta(minutes=15)
        await db.upsert_id_status(self.pool, torrent_id, status, http_status, next_retry)
        return kind, None

    async def frontier_cycle(self, lookahead: int, dry_streak: int, batch_concurrency: int):
        state = await db.get_crawl_state(self.pool)
        # Fold in successes found by other cycles (e.g. the retry ledger) so the
        # cap tracks real growth. Without this the frontier would freeze once
        # dry_streak IDs ahead, never noticing new uploads found off the frontier.
        highest_success = max(state["highest_success_id"], await db.max_torrent_id(self.pool))

        start = state["frontier_scan_high"] + 1
        # Never probe more than dry_streak IDs beyond the newest real torrent:
        # everything above highest_success is a guaranteed miss until the site
        # publishes more, so marching further just burns the request budget on
        # non-existent future IDs. The retry ledger re-checks the frontier_missing
        # gap every few minutes to catch new uploads.
        end = min(start + lookahead - 1, highest_success + dry_streak)

        if start > end:
            if highest_success > state["highest_success_id"]:
                await db.update_crawl_state(self.pool, highest_success_id=highest_success)
                await db.reclassify_internal_gaps(self.pool, highest_success)
            log.info(
                "frontier dry: scanned to %d (%d past highest_success %d), holding",
                state["frontier_scan_high"], state["frontier_scan_high"] - highest_success, highest_success,
            )
            return

        sem = asyncio.Semaphore(batch_concurrency)

        async def worker(tid):
            async with sem:
                kind, _ = await self.resolve_id(tid, gap_relative_to_highest=False)
                return tid, kind

        results = await asyncio.gather(*(worker(tid) for tid in range(start, end + 1)))

        for tid, kind in results:
            if kind == "success":
                highest_success = max(highest_success, tid)

        await db.update_crawl_state(
            self.pool, frontier_scan_high=end, highest_success_id=highest_success
        )
        if highest_success > state["highest_success_id"]:
            await db.reclassify_internal_gaps(self.pool, highest_success)

        log.info("frontier cycle %d..%d done, highest_success_id=%d", start, end, highest_success)

    async def retry_due_ledger(self, limit: int, batch_concurrency: int):
        rows = await db.fetch_retry_due(self.pool, limit)
        if not rows:
            return
        sem = asyncio.Semaphore(batch_concurrency)
        state = await db.get_crawl_state(self.pool)
        highest = state["highest_success_id"]

        async def worker(row):
            async with sem:
                await self.resolve_id(row["torrent_id"], gap_relative_to_highest=row["torrent_id"] < highest)

        await asyncio.gather(*(worker(r) for r in rows))
        log.info("retried %d due ledger entries", len(rows))

    async def backward_drain_chunk(self, chunk_size: int, confirm_window: int, safety_margin: int, batch_concurrency: int):
        state = await db.get_crawl_state(self.pool)
        if state["backfill_completed_at"] is not None:
            return

        floor = state["contiguous_floor_id"]
        if floor is None:
            floor = state["highest_success_id"]

        chunk_start = max(floor - chunk_size, 1)
        if chunk_start >= floor:
            return

        sem = asyncio.Semaphore(batch_concurrency)

        async def worker(tid):
            async with sem:
                await self.resolve_id(tid, gap_relative_to_highest=True)

        ids = list(range(chunk_start, floor))
        await asyncio.gather(*(worker(tid) for tid in ids))

        new_floor = chunk_start
        await db.update_crawl_state(self.pool, contiguous_floor_id=new_floor)

        cutoff = state["backfill_cutoff_at"]
        if cutoff is None:
            return

        all_old, dated_count = await db.confirm_window_all_old(self.pool, new_floor, confirm_window, cutoff)
        anchor = state["backfill_confirm_anchor_id"]

        if all_old and anchor is None:
            anchor = new_floor
            await db.update_crawl_state(self.pool, backfill_confirm_anchor_id=anchor)
        elif not all_old:
            anchor = None
            await db.update_crawl_state(self.pool, backfill_confirm_anchor_id=None)

        if anchor is not None and (anchor - new_floor) >= safety_margin:
            await db.update_crawl_state(
                self.pool,
                backfill_completed_at=datetime.now(timezone.utc),
            )
            log.info("backfill complete: floor=%d cutoff=%s", new_floor, cutoff)
        else:
            log.info("backward drain %d..%d, floor now %d", chunk_start, floor, new_floor)

    async def refresh_cycle(self, limit: int, batch_concurrency: int):
        rows = await db.fetch_refresh_due(self.pool, limit)
        if not rows:
            return
        sem = asyncio.Semaphore(batch_concurrency)

        async def worker(row):
            async with sem:
                kind, data = await self.resolve_id(row["torrent_id"], gap_relative_to_highest=True)
                if kind not in ("success", "not_found", "redirect"):
                    await db.bump_next_refresh(
                        self.pool, row["torrent_id"], datetime.now(timezone.utc) + timedelta(hours=6)
                    )

        await asyncio.gather(*(worker(r) for r in rows))
        log.info("refreshed %d rows", len(rows))

    async def bootstrap_if_needed(self):
        state = await db.get_crawl_state(self.pool)
        desired_cutoff = datetime.now(timezone.utc) - timedelta(days=self.config["BACKFILL_DAYS"])

        if state["backfill_cutoff_at"] is None:
            highest_seen = await self._discover_topten_max()
            await db.update_crawl_state(
                self.pool,
                highest_success_id=max(state["highest_success_id"], 0),
                frontier_scan_high=max(state["frontier_scan_high"], highest_seen - 1, 0),
                backfill_cutoff_at=desired_cutoff,
                contiguous_floor_id=highest_seen if highest_seen else 1,
            )
            log.info("bootstrap: cutoff=%s frontier_scan_high~%d", desired_cutoff, highest_seen)
            return

        if desired_cutoff < state["backfill_cutoff_at"]:
            await db.update_crawl_state(
                self.pool,
                backfill_cutoff_at=desired_cutoff,
                backfill_completed_at=None,
                backfill_confirm_anchor_id=None,
            )
            log.info("BACKFILL_DAYS increased, cutoff moved back to %s, resuming backward walk", desired_cutoff)

    async def _discover_topten_max(self) -> int:
        url = f"{self.base_url}/torrents/topten/"
        try:
            async with self.limiter.slot() as limiter:
                async with self.session.get(url, timeout=15.0) as resp:
                    if resp.status != 200:
                        limiter.note_failure("server_error" if resp.status >= 500 else "blocked")
                        return 0
                    text = await resp.text()
                    limiter.note_success()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return 0

        ids = [int(m) for m in re.findall(r"/torrents/details/(\d+)", text)]
        return max(ids) if ids else 0

    async def run_cycle(self):
        cfg = self.config
        await self.retry_due_ledger(cfg["RETRY_BATCH_SIZE"], cfg["MAX_CONCURRENCY"])
        await self.frontier_cycle(cfg["FRONTIER_LOOKAHEAD"], cfg["FRONTIER_DRY_STREAK"], cfg["MAX_CONCURRENCY"])
        await self.backward_drain_chunk(
            cfg["BACKFILL_CHUNK_SIZE"], cfg["BACKFILL_CONFIRM_WINDOW"], cfg["BACKFILL_SAFETY_MARGIN_IDS"], cfg["MAX_CONCURRENCY"]
        )
        await self.refresh_cycle(cfg["REFRESH_BATCH_SIZE"], cfg["MAX_CONCURRENCY"])

    async def run_forever(self):
        await self.bootstrap_if_needed()
        while True:
            try:
                await self.run_cycle()
            except Exception:
                log.exception("cycle failed, continuing after interval")
            await asyncio.sleep(self.config["CYCLE_INTERVAL"])
