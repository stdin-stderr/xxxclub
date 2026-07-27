"""Unified crawler loop: frontier scan/extend -> backward drain chunk -> age-tiered refresh.
See implementation.md "Unified crawler loop".
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import db
from host_pool import HostPool
from scraper import (
    ParseError,
    details_path,
    looks_like_challenge,
    looks_like_details_page,
    looks_like_soft_404,
    parse_details_html,
)

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
    def __init__(self, config: dict, pool, session, hosts: HostPool):
        self.config = config
        self.pool = pool
        self.session = session
        self.hosts = hosts
        self.tz_name = config["SITE_TZ"]

    async def fetch_and_classify(self, torrent_id: int):
        """Fetch one details page from the next available host, classify the outcome.
        Returns (kind, data_or_none, http_status_str).
        kind in: 'success', 'not_found', 'redirect', 'parse_error', 'blocked', 'server_error',
                 'timeout', 'connection_error'.

        Host pacing and cooling are entirely HostPool's business; this only maps the
        Outcome onto the ledger vocabulary.
        """
        outcome = await self.hosts.fetch(
            details_path(torrent_id),
            validate=looks_like_details_page,
            is_blocked=looks_like_challenge,
        )

        if outcome.kind == "ok":
            try:
                data = parse_details_html(outcome.html, torrent_id, self.tz_name)
            except ParseError as exc:
                # A structurally valid page we could not parse is a data problem, not a
                # pacing signal -- the host is deliberately left uncooled (see implementation.md).
                log.warning("parse_error on %s: %s", torrent_id, exc)
                return "parse_error", None, "200-parse-error"
            return "success", data, "200"

        if outcome.kind == "unrecognized":
            # The site serves "not found" as 200 + errordiv, never as a real 404, so
            # this is the normal outcome for any ID that does not exist yet.
            if looks_like_soft_404(outcome.html or ""):
                return "not_found", None, "200-soft-404"
            log.warning("unrecognized 200 page on %s", torrent_id)
            return "parse_error", None, "200-no-structure"

        if outcome.kind == "challenge":
            return "blocked", None, "200-challenge"

        if outcome.kind == "unexpected_status":
            return "parse_error", None, outcome.http_status

        # not_found / redirect / blocked / server_error / timeout / connection_error
        return outcome.kind, None, outcome.http_status

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

    async def frontier_cycle(self, lookahead: int, dry_streak: int):
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

        async def worker(tid):
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

    async def retry_due_ledger(self, limit: int):
        rows = await db.fetch_retry_due(self.pool, limit)
        if not rows:
            return
        state = await db.get_crawl_state(self.pool)
        highest = state["highest_success_id"]

        async def worker(row):
            await self.resolve_id(row["torrent_id"], gap_relative_to_highest=row["torrent_id"] < highest)

        await asyncio.gather(*(worker(r) for r in rows))
        log.info("retried %d due ledger entries", len(rows))

    async def backward_drain_chunk(self, chunk_size: int, confirm_window: int, safety_margin: int):
        state = await db.get_crawl_state(self.pool)
        if state["backfill_completed_at"] is not None:
            return

        floor = state["contiguous_floor_id"]
        if floor is None:
            floor = state["highest_success_id"]

        chunk_start = max(floor - chunk_size, 1)
        if chunk_start >= floor:
            return

        async def worker(tid):
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

    async def refresh_cycle(self, limit: int):
        rows = await db.fetch_refresh_due(self.pool, limit)
        if not rows:
            return

        async def worker(row):
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
        # Goes through the pool like every other request, so discovery is paced and
        # cooled on the same terms as the crawl itself.
        outcome = await self.hosts.fetch("/torrents/topten/")
        if outcome.kind != "ok" or not outcome.html:
            log.warning("topten discovery failed: %s", outcome.kind)
            return 0

        ids = [int(m) for m in re.findall(r"/torrents/details/(\d+)", outcome.html)]
        return max(ids) if ids else 0

    async def run_cycle(self):
        cfg = self.config
        await self.retry_due_ledger(cfg["RETRY_BATCH_SIZE"])
        await self.frontier_cycle(cfg["FRONTIER_LOOKAHEAD"], cfg["FRONTIER_DRY_STREAK"])
        await self.backward_drain_chunk(
            cfg["BACKFILL_CHUNK_SIZE"], cfg["BACKFILL_CONFIRM_WINDOW"], cfg["BACKFILL_SAFETY_MARGIN_IDS"]
        )
        await self.refresh_cycle(cfg["REFRESH_BATCH_SIZE"])

    async def run_forever(self):
        await self.bootstrap_if_needed()
        while True:
            try:
                await self.run_cycle()
            except Exception:
                log.exception("cycle failed, continuing after interval")
            await asyncio.sleep(self.config["CYCLE_INTERVAL"])
