"""Standalone probe: find real safe concurrency/rate against xxxclub.to.

Independent of rate_limiter.py/crawler.py — hits fetch_details directly at fixed
concurrency+rate steps, holds each step for a window, reports first failure signal
(403/429/5xx/timeout/connection_error). Plain 404 is not a failure (see CLAUDE.md).

Run with scraper container stopped (docker compose stop scraper) so results aren't
polluted by concurrent crawler traffic.

Usage:
    python3 probe_limit.py [--id 450462] [--hold 20] [--max-concurrency 6] [--max-rate 5]
"""

import argparse
import asyncio
import time

import aiohttp
import itertools

from scraper import fetch_details, looks_like_details_page

FAILURE_STATUSES = {403, 429, 500, 502, 503, 504}


async def run_step(session, base_url, id_cycle, concurrency, rate, hold_seconds):
    sem = asyncio.Semaphore(concurrency)
    interval = concurrency / rate
    stop_at = time.monotonic() + hold_seconds
    ok = 0
    failures = []
    lock = asyncio.Lock()

    async def worker():
        nonlocal ok
        while time.monotonic() < stop_at:
            torrent_id = next(id_cycle)
            async with sem:
                try:
                    status, html, _, retry_after = await fetch_details(session, base_url, torrent_id)
                except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                    async with lock:
                        failures.append(("exception", type(exc).__name__))
                    continue
                if status in FAILURE_STATUSES:
                    async with lock:
                        failures.append((status, retry_after))
                elif status == 200 and not looks_like_details_page(html) and html is not None:
                    async with lock:
                        failures.append(("challenge", "200-no-structure"))
                else:
                    async with lock:
                        ok += 1
            await asyncio.sleep(interval)

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)
    return ok, failures


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=450462, help="most recent id; probe walks backward from here")
    ap.add_argument("--spread", type=int, default=2000, help="how many distinct ids to cycle through")
    ap.add_argument("--base-url", default="https://xxxclub.to")
    ap.add_argument("--hold", type=float, default=20.0, help="seconds to hold each step")
    ap.add_argument("--max-concurrency", type=int, default=6)
    ap.add_argument("--max-rate", type=float, default=5.0)
    ap.add_argument("--cooldown", type=float, default=15.0, help="seconds to wait between steps")
    args = ap.parse_args()

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    steps = [(c, min(float(c) - 1 or 1.0, args.max_rate)) for c in range(1, args.max_concurrency + 1)]
    id_pool = list(range(args.id - args.spread, args.id + 1))

    async with aiohttp.ClientSession(headers=headers) as session:
        for concurrency, rate in steps:
            print(f"\n--- step: concurrency={concurrency} rate={rate:.2f}/s hold={args.hold}s ---", flush=True)
            id_cycle = itertools.cycle(id_pool)
            ok, failures = await run_step(session, args.base_url, id_cycle, concurrency, rate, args.hold)
            print(f"ok={ok} failures={len(failures)}")
            if failures:
                print(f"first failure: {failures[0]}")
                print("STOPPING — this step broke the limit. Last clean step was the previous one.")
                return
            print("clean, cooling down before next step...")
            await asyncio.sleep(args.cooldown)

    print("\nAll steps clean up to max-concurrency/max-rate. Limit is higher than tested range.")


if __name__ == "__main__":
    asyncio.run(main())
