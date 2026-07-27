"""Standalone probe: is the throttle per Cloudflare zone or at the shared origin?

Independent of host_pool.py/crawler.py — hits fetch_details directly at fixed rates so
results are not shaped by the crawler's own pacing. Plain 404 is not a failure
(see CLAUDE.md).

The three domains are three Cloudflare zones in front of one origin, so a clean
fan-out on its own proves nothing: it is equally consistent with "three zone budgets"
and with "the origin was always fine at 3 rps". The single-host control at the same
total rate is what separates them.

  1. baseline  -- 1 rps on one host        (is the environment clean?)
  2. control   -- 3 rps on one host        (does the origin tolerate the total?)
  3. fan-out   -- 1 rps x 3 hosts          (does splitting by zone help?)

  control fails + fan-out clean -> zone-level limit; the fan-out is doing real work
  control clean + fan-out clean -> origin tolerates 3 rps anyway; headroom exists
  both fail                     -> 3 rps is too much however it is routed; lower the rate
  control clean + fan-out fails -> .cc/.me are tuned tighter than .to; lower the rate

Every outcome still ships three hosts (see implementation.md "Probe methodology") — what changes is the per-domain
rate, not the design.

Run with the scraper stopped (docker compose stop scraper) so the crawler's own traffic
does not pollute the measurement.

Usage:
    python3 probe_limit.py --compare
    python3 probe_limit.py --id 450462 --hold 120
"""

import argparse
import asyncio
import itertools
import time

import aiohttp

from scraper import fetch_details, looks_like_details_page

FAILURE_STATUSES = {403, 429, 500, 502, 503, 504}
DOMAINS = ["https://xxxclub.to", "https://xxxclub.cc", "https://xxxclub.me"]


async def pace(base_urls, id_cycle, rate_per_host, hold_seconds, session, results):
    """One paced worker per host. Each issues `rate_per_host` requests/second."""
    interval = 1.0 / rate_per_host
    stop_at = time.monotonic() + hold_seconds

    async def worker(base_url):
        next_start = time.monotonic()
        while time.monotonic() < stop_at:
            now = time.monotonic()
            if next_start > now:
                await asyncio.sleep(next_start - now)
            next_start = time.monotonic() + interval
            torrent_id = next(id_cycle)
            try:
                status, html, _, retry_after = await fetch_details(session, base_url, torrent_id)
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                results.setdefault(base_url, []).append(("exception", type(exc).__name__))
                continue
            if status in FAILURE_STATUSES:
                results.setdefault(base_url, []).append((status, retry_after))
            elif status == 200 and html is not None and not looks_like_details_page(html):
                results.setdefault(base_url, []).append(("challenge", "200-no-structure"))
            else:
                results.setdefault(base_url, []).append(("ok", status))

    await asyncio.gather(*(worker(u) for u in base_urls))


def summarize(results):
    ok = 0
    failures = []
    for base_url, entries in results.items():
        for kind, detail in entries:
            if kind == "ok":
                ok += 1
            else:
                failures.append((base_url, kind, detail))
    return ok, failures


async def run_step(name, base_urls, rate_per_host, hold, id_cycle, session):
    total = rate_per_host * len(base_urls)
    print(f"\n--- {name}: {rate_per_host:g} rps x {len(base_urls)} host(s) "
          f"= {total:g} rps total, hold {hold:g}s ---", flush=True)
    results: dict[str, list] = {}
    await pace(base_urls, id_cycle, rate_per_host, hold, session, results)
    ok, failures = summarize(results)
    per_host = {u: len(v) for u, v in results.items()}
    print(f"ok={ok} failures={len(failures)} requests_per_host={per_host}")
    if failures:
        for base_url, kind, detail in failures[:5]:
            print(f"  FAIL {base_url}: {kind} {detail}")
    return not failures


async def recovery_check(session, torrent_id):
    """All three hosts must answer 200 before the next step, so a step never inherits
    a soured reputation from the previous one."""
    print("\nrecovery check...", flush=True)
    healthy = True
    for base_url in DOMAINS:
        try:
            status, html, _, _ = await fetch_details(session, base_url, torrent_id)
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            print(f"  {base_url}: {type(exc).__name__}")
            healthy = False
            continue
        good = status == 200 and html is not None and looks_like_details_page(html)
        print(f"  {base_url}: {status}{'' if good else '  <-- NOT HEALTHY'}")
        healthy = healthy and good
        await asyncio.sleep(2)
    return healthy


def verdict(control_clean, fanout_clean):
    if not control_clean and fanout_clean:
        return ("Zone-level limit: the fan-out is doing real work.\n"
                "  -> BASE_URLS with all three, REQUESTS_PER_SECOND_PER_DOMAIN=1")
    if control_clean and fanout_clean:
        return ("Origin tolerates 3 rps by any routing; there is headroom.\n"
                "  -> BASE_URLS with all three at 1 rps, and probe upward later")
    if not control_clean and not fanout_clean:
        return ("3 rps total is too much however it is routed.\n"
                "  -> BASE_URLS with all three, REQUESTS_PER_SECOND_PER_DOMAIN=0.33 "
                "(1 rps total), then probe upward")
    return ("The extra hosts are tuned tighter than .to.\n"
            "  -> BASE_URLS with all three at a reduced rate, or drop the tighter hosts")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=450462, help="most recent id; probe walks backward from here")
    ap.add_argument("--spread", type=int, default=2000, help="how many distinct ids to cycle through")
    ap.add_argument("--hold", type=float, default=120.0, help="seconds to hold each step")
    ap.add_argument("--cooldown", type=float, default=60.0, help="seconds between steps")
    ap.add_argument("--compare", action="store_true", help="run the three-step comparison")
    args = ap.parse_args()

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    id_pool = list(range(args.id - args.spread, args.id + 1))
    id_cycle = itertools.cycle(id_pool)

    async with aiohttp.ClientSession(headers=headers) as session:
        if not args.compare:
            await run_step("baseline", DOMAINS[:1], 1.0, args.hold, id_cycle, session)
            return

        if not await run_step("1. baseline", DOMAINS[:1], 1.0, args.hold, id_cycle, session):
            print("\nBaseline is already failing — the environment is not clean. Stopping.")
            return

        if not await recovery_check(session, args.id):
            print("\nNot healthy after the baseline. Stopping.")
            return
        await asyncio.sleep(args.cooldown)

        control_clean = await run_step("2. control", DOMAINS[:1], 3.0, args.hold, id_cycle, session)

        if not await recovery_check(session, args.id):
            print("\nNot healthy after the control step. Wait 15+ min before step 3.")
            return
        await asyncio.sleep(args.cooldown)

        fanout_clean = await run_step("3. fan-out", DOMAINS, 1.0, args.hold, id_cycle, session)
        await recovery_check(session, args.id)

        print("\n" + "=" * 60)
        print(f"control (3 rps, one host): {'clean' if control_clean else 'FAILED'}")
        print(f"fan-out (1 rps x 3 hosts): {'clean' if fanout_clean else 'FAILED'}")
        print(verdict(control_clean, fanout_clean))


if __name__ == "__main__":
    asyncio.run(main())
