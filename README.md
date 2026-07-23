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

The first TPDB pass deliberately uses only the largest file in each torrent's
`files` array and sends its filename through TPDB's `parse` scene search. The
first returned scene is stored; matched, unmatched, errored, and file-less
attempts are all recorded in `tpdb_match_attempts` so coverage is measurable.
