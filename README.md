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
- `scraper` — rate-limited crawler, using the shared `xxxclub:local` image
- `web` — read-only archive UI, using the shared `xxxclub:local` image
- `tpdb-matcher` — rate-limited filename matcher, using the shared `xxxclub:local` image

Compose selects the application task through the image command: `scraper`,
`web`, or `tpdb-matcher`.

To run the image published by GitHub Actions, use
[`docker-compose.ghcr.yml`](docker-compose.ghcr.yml):

```sh
cp .env.example .env
docker compose -f docker-compose.ghcr.yml up -d
```

Pushes to `main` publish `latest`, `main`, and a commit SHA tag to GHCR.
Version tags such as `v1.2.3` also publish `1.2.3` and `1.2`.

Configuration lives in `.env`; see `.env.example` for the available settings.

The TPDB matcher prefers the largest video file, normalizes resolution siblings,
and reuses an already verified sibling match when possible. It then tries TPDB's
filename parser, site-specific recent scenes, and conservative text-search
fallbacks. Candidates are scored from site, release date, title, and performer
evidence; ambiguous results remain unmatched and are retried with backoff.
`python3 tpdb_matcher.py --dry-run --once` evaluates one batch without persisting
match outcomes.

Use `--limit N` for a larger one-pass shadow evaluation, or
`--torrent-ids ID [ID ...]` to evaluate an explicit set without repeatedly
fetching the same first batch. Dry-run results are logged as structured JSON.
