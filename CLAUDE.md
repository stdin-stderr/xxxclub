# xxxclub V2 scraper

Scrapes `xxxclub.to/torrents/details/{id}` (sequential numeric IDs) into Postgres.
It also serves a read-only archive and runs a separate TPDB filename matcher for
scene categories. The matcher is intentionally isolated from crawl/rate-limit
state.

`xxxclub.to`, `.cc` and `.me` are three Cloudflare zones in front of **one origin**,
serving the same ID space — any host can serve any torrent. `BASE_URLS` lists which
ones to use.

Full design rationale, data model, and open work live in [implementation.md](implementation.md) —
read it before changing crawl logic, schema, or rate limiting. Reference fixture:
`450462.html` (saved details page, used to build/test the parser without network calls).

## Layout

- `db.py` — schema (`torrents`, `id_status`, `crawl_state`, `tpdb_*`) + upsert helpers
- `config.py` — env loading + validation, no DB/HTTP imports so it is testable alone
- `host_pool.py` — queue of hosts: fixed per-domain pacing, per-host cooldowns (independent of scraper logic)
- `scraper.py` — fetch + parse one details page; `parse_details_html()` is pure/offline-testable against the fixture
- `crawler.py` — unified loop: frontier scan/extend → backward drain chunk → age-tiered refresh
- `entrypoint.py` — wires config + pool + crawler together and starts the loop
- `probe_limit.py` — standalone: is the throttle per zone or at the origin? (`--compare`)
- `tpdb_client.py` — TPDB request pacing, largest-file selection, response mapping
- `tpdb_matcher.py` — independent matching loop + outcome coverage logging
- `webapp.py` / `web/templates/` — torrent and TPDB entity archive/detail pages

## Running

```bash
docker compose up -d
```

```bash
docker compose logs scraper --tail 50 --no-log-prefix
```

```bash
docker compose exec db psql -U xxxclub -d xxxclub -c "SELECT * FROM crawl_state;"
```

`backfill_completed_at` in `crawl_state` flips from null once the `BACKFILL_DAYS` window is fully walked and confirmed.

Config is `.env` (copy from `.env.example`). Raising `BACKFILL_DAYS` later resumes the backward walk further rather than restarting.

## Key invariants (don't casually change)

- `torrents` = successes only. `id_status` = everything else (`frontier_missing`/`internal_gap`/`gone`/`transient_error`/`parse_error`), disjoint PK space.
- `crawl_state` is the single source of truth for crawl progress — never infer progress from row contents.
- Pacing is per domain (`REQUESTS_PER_SECOND_PER_DOMAIN`). Concurrency is **not** a separate knob: each host sits in an `asyncio.Queue` exactly once, so total in-flight equals the number of `BASE_URLS`, and total throughput is rate × domains. Fractional rates are supported (`0.5` = one request per 2s).
- **The site never returns a real `404`.** A missing ID gets HTTP `200` with the normal chrome plus `<div class="errordiv">…404 : Not Found`. `looks_like_soft_404()` detects it and it is treated as `not_found` (`frontier_missing`/`internal_gap`), exactly like a real 404 would be.
- Only `403`/`429` and a **positively identified** challenge/interstitial cool a host, plus a brief cool on `5xx`/timeout/connection error. A `200` that is merely unrecognized does *not* cool a host — guessing that an unfamiliar page is a block stalls the frontier, since most unrecognized 200s are just soft-404s. A parse failure on a structurally valid page also does not cool a host: that's a data problem, not a pacing signal.
- Cloudflare's `/cdn-cgi/challenge-platform/` beacon script is on **every** page including valid ones — never match on the bare word "challenge". `CHALLENGE_MARKERS` in `scraper.py` lists interstitial-only strings.
- There is no adaptive ramping and no global circuit breaker. A cooling host is absent from the queue; if all hosts are cooling, callers wait and one log line is emitted on the transition (and one more when it clears).
- Never fetch the same `torrent_id` from two hosts at once, and never retry a blocked ID on a different host — it belongs in the retry ledger.
- No binary search on date for the backfill boundary — ID-existence isn't monotonic against date.
- Redirects are not auto-followed (`allow_redirects=False`). A `200` that doesn't match the details structure is never a success; which non-success it is depends on the page — soft-404 → `not_found`, positively-identified interstitial → `blocked`, anything else → `parse_error`.
- Images use `/1s/` (thumbnail) → `/1/` (full-res) URL substitution; magnets are taken verbatim from the page rather than rebuilt, avoiding any percent-encoding of the `btih:` colons.

## Schema notes

- `tags` is `text[]` (display text only, no href).
- `added_at` is `NOT NULL` — the site always fills "Added Date"; no raw-text fallback column is kept (unlike `last_scraped`, which can legitimately be null on `"Pending"`). A page where Added Date fails to parse is a `parse_error`, not a success with a null date.
- Refresh cadence has no separate `<24h` tier — `<7d` (including brand-new torrents) all refresh every 6h; see `REFRESH_TIERS` in `crawler.py`.
- `description_text` (plain text, screenshots/MediaInfo stripped) + `media_info` (jsonb, parsed from the `.description`'s monospace MediaInfo block; `null` when a release has no MediaInfo, e.g. image-only posts). No `description_html` or `raw_html` column is kept.
- Schema changes so far were applied via full re-ingest (`docker compose down -v` + fresh `up -d`), not in-place migrations — there is no migration path in `db.py` currently. Add one deliberately if a future schema change needs to preserve existing data.
- TPDB tables are an additive schema extension created with `CREATE TABLE IF NOT
  EXISTS`, so enabling matching does not require wiping the existing torrent
  volume.
- TPDB scenes keep a dedicated `background_url` for 16:9 archive/detail artwork;
  existing rows are backfilled from their retained TPDB `backgrounds`/`metadata`
  JSON. Performer detail galleries decode and deduplicate the stored primary,
  face, thumbnail, and poster URLs.
- TPDB matching prefers video files, collapses resolution suffixes into a scene
  key, and reuses verified sibling matches. API candidates come from filename,
  site-recent-scene, and text-query searches and must pass conservative
  site/date/title/performer scoring. Unmatched results are retried with backoff;
  `--dry-run` performs a one-batch shadow evaluation without outcome writes.
- No `source_domain` column — a schema change would force a full re-ingest, so which host served a request is logged, not stored.

## Testing

Unit tests are offline — no network, no DB, no Docker. `test_host_pool.py` drives
pacing and cooldowns off a fake clock, so timing assertions are exact and instant:

```bash
python3 -m unittest discover -p "test_*.py"
```

Parser can be checked offline against the saved fixture, no network/DB needed:

```bash
python3 -c "
from scraper import parse_details_html
html = open('450462.html').read()
print(parse_details_html(html, 450462))
"
```

Before changing `BASE_URLS` to more than one host, run the probe with the scraper
stopped — it decides the safe per-domain rate (see [implementation.md](implementation.md) §Probe):

```bash
python3 probe_limit.py --compare
```
