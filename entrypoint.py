"""Startup: load config from env, log active config with secrets masked, run crawler loop.
V1's entrypoint.py dumped the full environment (including DB password) on startup — deliberately not repeated here.
"""

import asyncio
import logging
import os
import sys
from urllib.parse import quote_plus

import aiohttp

import db
from crawler import Crawler
from rate_limiter import AdaptiveLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("entrypoint")

SECRET_KEYS = {"POSTGRES_PASSWORD"}


def env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def load_config() -> dict:
    return {
        "POSTGRES_USER": os.environ.get("POSTGRES_USER", "xxxclub"),
        "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", "xxxclub"),
        "POSTGRES_DB": os.environ.get("POSTGRES_DB", "xxxclub"),
        "POSTGRES_HOST": os.environ.get("POSTGRES_HOST", "db"),
        "POSTGRES_PORT": env_int("POSTGRES_PORT", 5432),
        "BASE_URL": os.environ.get("BASE_URL", "https://xxxclub.to"),
        "SITE_TZ": os.environ.get("SITE_TZ", "UTC"),
        "BACKFILL_DAYS": env_int("BACKFILL_DAYS", 30),
        "MAX_CONCURRENCY": env_int("MAX_CONCURRENCY", 2),
        "MAX_CONCURRENCY_CEILING": env_int("MAX_CONCURRENCY_CEILING", 8),
        "MAX_REQUESTS_PER_SECOND": env_float("MAX_REQUESTS_PER_SECOND", 1.0),
        "MAX_REQUESTS_PER_SECOND_CEILING": env_float("MAX_REQUESTS_PER_SECOND_CEILING", 4.0),
        "FRONTIER_LOOKAHEAD": env_int("FRONTIER_LOOKAHEAD", 500),
        "FRONTIER_DRY_STREAK": env_int("FRONTIER_DRY_STREAK", 200),
        "BACKFILL_CHUNK_SIZE": env_int("BACKFILL_CHUNK_SIZE", 500),
        "BACKFILL_CONFIRM_WINDOW": env_int("BACKFILL_CONFIRM_WINDOW", 100),
        "BACKFILL_SAFETY_MARGIN_IDS": env_int("BACKFILL_SAFETY_MARGIN_IDS", 200),
        "RETRY_BATCH_SIZE": env_int("RETRY_BATCH_SIZE", 200),
        "REFRESH_BATCH_SIZE": env_int("REFRESH_BATCH_SIZE", 200),
        "CYCLE_INTERVAL": env_int("CYCLE_INTERVAL", 60),
    }


def log_config(cfg: dict) -> None:
    masked = {k: ("***" if k in SECRET_KEYS else v) for k, v in cfg.items()}
    for k, v in masked.items():
        log.info("config %s = %s", k, v)


def dsn(cfg: dict) -> str:
    user = quote_plus(cfg['POSTGRES_USER'])
    password = quote_plus(cfg['POSTGRES_PASSWORD'])
    return (
        f"postgresql://{user}:{password}"
        f"@{cfg['POSTGRES_HOST']}:{cfg['POSTGRES_PORT']}/{cfg['POSTGRES_DB']}"
    )


async def main():
    cfg = load_config()
    log_config(cfg)

    pool = await db.create_pool(dsn(cfg))
    try:
        await db.init_schema(pool)
        log.info("schema ready")

        limiter = AdaptiveLimiter(
            min_concurrency=cfg["MAX_CONCURRENCY"],
            max_concurrency=cfg["MAX_CONCURRENCY_CEILING"],
            min_rate=cfg["MAX_REQUESTS_PER_SECOND"],
            max_rate=cfg["MAX_REQUESTS_PER_SECOND_CEILING"],
        )

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession(headers=headers) as session:
            crawler = Crawler(cfg, pool, session, limiter)
            await crawler.run_forever()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
