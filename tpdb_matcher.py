"""Continuously match eligible torrents to TPDB scenes using the largest filename."""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import aiohttp

import db
from tpdb_client import (
    TPDBClient,
    TPDBError,
    build_match_source,
    choose_candidate,
    largest_file,
    map_scene_record,
    select_site,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tpdb_matcher")


def dsn_from_env() -> str:
    user = quote_plus(os.environ.get('POSTGRES_USER', 'xxxclub'))
    password = quote_plus(os.environ.get('POSTGRES_PASSWORD', 'xxxclub'))
    return (
        f"postgresql://{user}:"
        f"{password}"
        f"@{os.environ.get('POSTGRES_HOST', 'db')}:{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('POSTGRES_DB', 'xxxclub')}"
    )


def decode_files(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def unmatched_retry_at(attempts: int):
    if attempts < 2:
        delay = timedelta(hours=6)
    elif attempts < 4:
        delay = timedelta(days=1)
    elif attempts < 7:
        delay = timedelta(days=7)
    else:
        delay = timedelta(days=30)
    return datetime.now(timezone.utc) + delay


def _annotate_audit(audit: list[dict], method: str, query: str | None) -> list[dict]:
    return [
        {**item, "method": method, "query": query}
        for item in audit
    ]


def _audit_for_storage(audit: list[dict], limit: int = 25) -> list[dict]:
    """Keep chronological audit order while guaranteeing the selected row survives."""

    stored = audit[:limit]
    selected = next((item for item in audit if item.get("selected")), None)
    if selected is not None and selected not in stored:
        if stored:
            stored[-1] = selected
        else:
            stored.append(selected)
    return stored


def _log_dry_run(payload: dict) -> None:
    log.info("dry-run result %s", json.dumps(payload, sort_keys=True, default=str))


async def search_with_fallbacks(client: TPDBClient, source, filename: str) -> dict:
    inspected = 0
    audit = []
    last_reason = "no candidates"

    candidates, _ = await client.search_filename(filename)
    inspected += len(candidates)
    decision = choose_candidate(candidates, source)
    audit.extend(_annotate_audit(decision.audit, "parse_filename", filename))
    last_reason = decision.reason
    if decision.accepted:
        return {
            "candidate": decision.candidate,
            "score": decision.score,
            "method": "parse_filename",
            "query": filename,
            "candidate_count": inspected,
            "audit": audit,
            "reason": decision.reason,
        }

    sites, _ = await client.search_sites(source.site_label)
    site = select_site(sites, source.site_label)
    if site is not None and site.get("id") is not None:
        candidates, _ = await client.site_scenes(int(site["id"]))
        inspected += len(candidates)
        decision = choose_candidate(candidates, source, expected_site=site)
        audit.extend(
            _annotate_audit(
                decision.audit,
                "site_recent_scenes",
                str(site["id"]),
            )
        )
        last_reason = decision.reason
        if decision.accepted:
            return {
                "candidate": decision.candidate,
                "score": decision.score,
                "method": "site_recent_scenes",
                "query": str(site["id"]),
                "candidate_count": inspected,
                "audit": audit,
                "reason": decision.reason,
            }

    for query in source.queries[:4]:
        candidates, _ = await client.search_scenes(query)
        inspected += len(candidates)
        decision = choose_candidate(candidates, source, expected_site=site)
        audit.extend(_annotate_audit(decision.audit, "scene_query", query))
        last_reason = decision.reason
        if decision.accepted:
            return {
                "candidate": decision.candidate,
                "score": decision.score,
                "method": "scene_query",
                "query": query,
                "candidate_count": inspected,
                "audit": audit,
                "reason": decision.reason,
            }

    return {
        "candidate": None,
        "score": 0.0,
        "method": "fallback_exhausted",
        "query": source.queries[-1] if source.queries else filename,
        "candidate_count": inspected,
        "audit": audit,
        "reason": last_reason,
    }


async def match_one(pool, client: TPDBClient, row, *, dry_run: bool = False) -> str:
    selected = largest_file(decode_files(row["files"]))
    if selected is None:
        if dry_run:
            _log_dry_run(
                {
                    "status": "no_file",
                    "torrent_id": row["torrent_id"],
                    "reason": "torrent has no usable file entry",
                }
            )
        if not dry_run:
            await db.record_tpdb_match_outcome(
                pool,
                torrent_id=row["torrent_id"],
                filename=None,
                file_size_bytes=None,
                status="no_file",
                last_error="torrent has no usable file entry",
                method="video_file_selection",
            )
        return "no_file"

    filename, size_bytes = selected
    source = build_match_source(filename, row["title"], row["category"])
    try:
        reusable = await db.find_reusable_tpdb_match(
            pool,
            source.scene_key,
            row["torrent_id"],
        )
        if reusable:
            if dry_run:
                _log_dry_run(
                    {
                        "status": "matched",
                        "torrent_id": row["torrent_id"],
                        "filename": filename,
                        "scene_id": reusable["scene_id"],
                        "method": "resolution_sibling",
                        "source_torrent_id": reusable["source_torrent_id"],
                        "score": float(reusable["match_score"]),
                    }
                )
            else:
                await db.save_reused_tpdb_match(
                    pool,
                    torrent_id=row["torrent_id"],
                    scene_id=reusable["scene_id"],
                    filename=filename,
                    file_size_bytes=size_bytes,
                    scene_key=source.scene_key,
                    source_torrent_id=reusable["source_torrent_id"],
                    match_score=reusable["match_score"],
                )
            return "matched"

        result = await search_with_fallbacks(client, source, filename)
        if result["candidate"] is None:
            if dry_run:
                _log_dry_run(
                    {
                        "status": "unmatched",
                        "torrent_id": row["torrent_id"],
                        "filename": filename,
                        "method": result["method"],
                        "score": result["score"],
                        "candidate_count": result["candidate_count"],
                        "reason": result["reason"],
                    }
                )
                return "unmatched"
            await db.record_tpdb_match_outcome(
                pool,
                torrent_id=row["torrent_id"],
                filename=filename,
                file_size_bytes=size_bytes,
                status="unmatched",
                candidate_count=result["candidate_count"],
                http_status=200,
                last_error=result["reason"][:500],
                next_retry_at=unmatched_retry_at(row["attempts"]),
                method=result["method"],
                scene_key=source.scene_key,
                search_query=result["query"],
                match_score=result["score"],
                candidate_metadata=_audit_for_storage(result["audit"]),
            )
            return "unmatched"

        bundle = map_scene_record(result["candidate"])
        if dry_run:
            _log_dry_run(
                {
                    "status": "matched",
                    "torrent_id": row["torrent_id"],
                    "filename": filename,
                    "scene_id": bundle["scene"]["scene_id"],
                    "scene_title": bundle["scene"]["title"],
                    "method": result["method"],
                    "score": result["score"],
                    "candidate_count": result["candidate_count"],
                    "reason": result["reason"],
                }
            )
        else:
            await db.save_tpdb_match(
                pool,
                torrent_id=row["torrent_id"],
                filename=filename,
                file_size_bytes=size_bytes,
                candidate_count=result["candidate_count"],
                bundle=bundle,
                method=result["method"],
                scene_key=source.scene_key,
                search_query=result["query"],
                match_score=result["score"],
                candidate_metadata=_audit_for_storage(result["audit"]),
            )
        if not dry_run:
            log.info(
                "matched torrent %s (%s) -> %s via %s score=%.3f",
                row["torrent_id"],
                filename,
                bundle["scene"]["title"],
                result["method"],
                result["score"],
            )
        return "matched"
    except (TPDBError, ValueError) as exc:
        status = exc.status if isinstance(exc, TPDBError) else 200
        retry_delay = timedelta(hours=1)
        if status == 429:
            retry_delay = timedelta(minutes=15)
        if dry_run:
            _log_dry_run(
                {
                    "status": "error",
                    "torrent_id": row["torrent_id"],
                    "filename": filename,
                    "http_status": status,
                    "reason": str(exc),
                }
            )
        if not dry_run:
            await db.record_tpdb_match_outcome(
                pool,
                torrent_id=row["torrent_id"],
                filename=filename,
                file_size_bytes=size_bytes,
                status="error",
                http_status=status,
                last_error=str(exc)[:500],
                next_retry_at=datetime.now(timezone.utc) + retry_delay,
                method="request_error",
                scene_key=source.scene_key,
            )
        log.warning("torrent %s TPDB lookup failed: %s", row["torrent_id"], exc)
        return "error"


async def log_stats(pool) -> None:
    stats = await db.get_tpdb_match_stats(pool)
    log.info(
        "coverage eligible=%d attempted=%d matched=%d unmatched=%d "
        "errors=%d no_file=%d pending=%d match_rate=%.1f%%",
        stats["eligible"],
        stats["attempted"],
        stats["matched"],
        stats["unmatched"],
        stats["errors"],
        stats["no_file"],
        stats["pending"],
        stats["match_rate"],
    )


async def run(
    run_once: bool = False,
    dry_run: bool = False,
    *,
    limit: int | None = None,
    torrent_ids: list[int] | None = None,
) -> None:
    api_key = os.environ.get("THEPORNDB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("THEPORNDB_API_KEY is required")

    batch_size = (
        limit
        or (len(torrent_ids) if torrent_ids else 0)
        or int(os.environ.get("TPDB_BATCH_SIZE", "25"))
    )
    cycle_interval = int(os.environ.get("TPDB_CYCLE_INTERVAL", "60"))
    rate = float(os.environ.get("TPDB_REQUESTS_PER_SECOND", "1"))

    pool = await db.create_pool(dsn_from_env())
    await db.init_schema(pool)
    if not dry_run:
        migration = await db.migrate_tpdb_matcher_v2(pool)
        log.info(
            "TPDB matcher V2 data migration: "
            "legacy_deleted=%d siblings_backfilled=%d",
            migration["legacy_deleted"],
            migration["siblings_backfilled"],
        )
    log.info(
        "schema ready; matching categories=%s at %.2f request(s)/second",
        ", ".join(db.TPDB_CATEGORIES),
        rate,
    )

    headers = {"User-Agent": "xxxclub-tpdb-matcher/1.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            client = TPDBClient(session, api_key, rate)
            while True:
                rows = (
                    await db.fetch_tpdb_shadow_candidates(
                        pool,
                        batch_size,
                        torrent_ids=torrent_ids,
                    )
                    if dry_run
                    else await db.fetch_tpdb_match_candidates(pool, batch_size)
                )
                if not rows:
                    await log_stats(pool)
                    if run_once:
                        return
                    await asyncio.sleep(cycle_interval)
                    continue

                outcomes = {"matched": 0, "unmatched": 0, "error": 0, "no_file": 0}
                for row in rows:
                    outcome = await match_one(pool, client, row, dry_run=dry_run)
                    outcomes[outcome] += 1
                log.info("batch complete: %s", outcomes)
                await log_stats(pool)
                if dry_run:
                    return
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain all currently eligible rows and exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate one batch without persisting match outcomes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="override TPDB_BATCH_SIZE (especially useful for one-pass dry runs)",
    )
    parser.add_argument(
        "--torrent-ids",
        type=int,
        nargs="+",
        help="evaluate only these torrent IDs (requires --dry-run)",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.torrent_ids and not args.dry_run:
        parser.error("--torrent-ids requires --dry-run")
    asyncio.run(
        run(
            run_once=args.once,
            dry_run=args.dry_run,
            limit=args.limit,
            torrent_ids=args.torrent_ids,
        )
    )
