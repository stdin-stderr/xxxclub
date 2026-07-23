# xxxclub

Small PostgreSQL-backed crawler and read-only web archive for xxxclub torrent details.

## Run with Docker

```sh
cp .env.example .env
docker compose up --build
```

The web UI is available at <http://localhost:8080>. The crawler stores parsed records in PostgreSQL and continuously handles discovery, backfill, retries, and refreshes.

## Services

- `db` — PostgreSQL 16 database
- `scraper` — rate-limited crawler
- `web` — read-only torrent and TPDB catalog archive/detail pages
- `tpdb-matcher` — rate-limited filename matcher for supported scene categories

Configuration lives in `.env`; see `.env.example` for the available settings.

The TPDB matcher prefers the largest video file, normalizes resolution siblings,
and reuses an already verified sibling match when possible. It then tries TPDB's
filename parser, site-specific recent scenes, and conservative text-search
fallbacks. Candidates are scored from site, release date, title, and performer
evidence; ambiguous results remain unmatched and are retried with backoff.
`python3 tpdb_matcher.py --dry-run --once` evaluates one batch without persisting
match outcomes.
