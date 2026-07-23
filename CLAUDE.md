# xxxclub V2 scraper

Scrapes `xxxclub.to/torrents/details/{id}` (sequential numeric IDs) into Postgres.
It also serves a read-only archive and runs a separate TPDB filename matcher for
scene categories. The matcher is intentionally isolated from crawl/rate-limit
state.

Full design rationale lives in [PLAN.md](PLAN.md) — read it before changing crawl logic, schema, or rate limiting. Reference fixture: `450462.html` (saved details page, used to build/test the parser without network calls).

## Layout

- `db.py` — schema (`torrents`, `id_status`, `crawl_state`) + upsert helpers
- `rate_limiter.py` — token bucket + concurrency semaphore + circuit breaker (independent of scraper logic)
- `scraper.py` — fetch + parse one details page; `parse_details_html()` is pure/offline-testable against the fixture
- `crawler.py` — unified loop: frontier scan/extend → backward drain chunk → age-tiered refresh
- `entrypoint.py` — env config (masked secrets in logs) + starts the crawler loop
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
- `MAX_CONCURRENCY` and `MAX_REQUESTS_PER_SECOND` are independent knobs; circuit breaker trips on 403/429/5xx/timeout/parse_error spikes, not on plain 404.
- No binary search on date for the backfill boundary — ID-existence isn't monotonic against date.
- Redirects are not auto-followed (`allow_redirects=False`); a `200` that doesn't match expected structure is `parse_error`, not success.

## Schema notes (post-initial-build corrections)

- `tags` is `text[]` (display text only, no href) — changed from an earlier `jsonb [{text,href}]` design.
- `added_at` is `NOT NULL` — the site always fills "Added Date"; no raw-text fallback column is kept (unlike `last_scraped`, which can legitimately be null on `"Pending"`). A page where Added Date fails to parse is a `parse_error`, not a success with a null date.
- Refresh cadence has no separate `<24h` tier — `<7d` (including brand-new torrents) all refresh every 6h; see `REFRESH_TIERS` in `crawler.py`.
- `description_html` was dropped in favor of `description_text` (plain text, screenshots/MediaInfo stripped) + `media_info` (jsonb, parsed from the `.description`'s monospace MediaInfo block; `null` when a release has no MediaInfo, e.g. image-only posts). See [PLAN.md](PLAN.md) Retention decision.
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

## Testing

Parser can be checked offline against the saved fixture, no network/DB needed:

```bash
python3 -c "
from scraper import parse_details_html
html = open('450462.html').read()
print(parse_details_html(html, 450462))
"
```
