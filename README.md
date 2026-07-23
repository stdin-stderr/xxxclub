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
- `web` — read-only archive and detail pages

Configuration lives in `.env`; see `.env.example` for the available settings.
