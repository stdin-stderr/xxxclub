"""Continuously match eligible torrents to TPDB scenes using the largest filename."""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

import db
from tpdb_client import TPDBClient, TPDBError, largest_file, map_scene_record

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tpdb_matcher")


def dsn_from_env() -> str:
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'xxxclub')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'xxxclub')}"
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


async def match_one(pool, client: TPDBClient, row) -> str:
    selected = largest_file(decode_files(row["files"]))
    if selected is None:
        await db.record_tpdb_match_outcome(
            pool,
            torrent_id=row["torrent_id"],
            filename=None,
            file_size_bytes=None,
            status="no_file",
            last_error="torrent has no usable file entry",
        )
        return "no_file"

    filename, size_bytes = selected
    try:
        candidates, candidate_count = await client.search_filename(filename)
        if not candidates:
            await db.record_tpdb_match_outcome(
                pool,
                torrent_id=row["torrent_id"],
                filename=filename,
                file_size_bytes=size_bytes,
                status="unmatched",
                candidate_count=0,
                http_status=200,
            )
            return "unmatched"

        bundle = map_scene_record(candidates[0])
        await db.save_tpdb_match(
            pool,
            torrent_id=row["torrent_id"],
            filename=filename,
            file_size_bytes=size_bytes,
            candidate_count=candidate_count,
            bundle=bundle,
        )
        log.info(
            "matched torrent %s (%s) -> %s",
            row["torrent_id"],
            filename,
            bundle["scene"]["title"],
        )
        return "matched"
    except (TPDBError, ValueError) as exc:
        status = exc.status if isinstance(exc, TPDBError) else 200
        retry_delay = timedelta(hours=1)
        if status == 429:
            retry_delay = timedelta(minutes=15)
        await db.record_tpdb_match_outcome(
            pool,
            torrent_id=row["torrent_id"],
            filename=filename,
            file_size_bytes=size_bytes,
            status="error",
            http_status=status,
            last_error=str(exc)[:500],
            next_retry_at=datetime.now(timezone.utc) + retry_delay,
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


async def run(run_once: bool = False) -> None:
    api_key = os.environ.get("THEPORNDB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("THEPORNDB_API_KEY is required")

    batch_size = int(os.environ.get("TPDB_BATCH_SIZE", "25"))
    cycle_interval = int(os.environ.get("TPDB_CYCLE_INTERVAL", "60"))
    rate = float(os.environ.get("TPDB_REQUESTS_PER_SECOND", "1"))

    pool = await db.create_pool(dsn_from_env())
    await db.init_schema(pool)
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
                rows = await db.fetch_tpdb_match_candidates(pool, batch_size)
                if not rows:
                    await log_stats(pool)
                    if run_once:
                        return
                    await asyncio.sleep(cycle_interval)
                    continue

                outcomes = {"matched": 0, "unmatched": 0, "error": 0, "no_file": 0}
                for row in rows:
                    outcome = await match_one(pool, client, row)
                    outcomes[outcome] += 1
                log.info("batch complete: %s", outcomes)
                await log_stats(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="drain all currently eligible rows and exit",
    )
    args = parser.parse_args()
    asyncio.run(run(run_once=args.once))
