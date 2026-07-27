"""Startup: load config from env, log active config with secrets masked, run crawler loop.
V1's entrypoint.py dumped the full environment (including DB password) on startup — deliberately not repeated here.
"""

import asyncio
import logging
import sys

import aiohttp

import db
from config import ConfigError, dsn, load_config, log_config
from crawler import Crawler
from host_pool import HostPool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("entrypoint")


async def main():
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        raise SystemExit(1)
    log_config(cfg)

    pool = await db.create_pool(dsn(cfg))
    try:
        await db.init_schema(pool)
        log.info("schema ready")

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with aiohttp.ClientSession(headers=headers) as session:
            hosts = HostPool(
                session=session,
                base_urls=cfg["BASE_URLS"],
                requests_per_second_per_domain=cfg["REQUESTS_PER_SECOND_PER_DOMAIN"],
                block_cooldown=cfg["BLOCK_COOLDOWN_SECONDS"],
                request_timeout=cfg["REQUEST_TIMEOUT_SECONDS"],
            )
            try:
                crawler = Crawler(cfg, pool, session, hosts)
                await crawler.run_forever()
            finally:
                hosts.close()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
