"""Postgres schema + data-access helpers. See PLAN.md Data model / Non-negotiables 1+2."""

import json

import asyncpg

JSONB_COLUMNS = {"files", "media_info"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS torrents (
    torrent_id        BIGINT PRIMARY KEY,
    info_hash         TEXT,
    title             TEXT NOT NULL,
    category          TEXT,
    size_bytes        BIGINT,
    added_at          TIMESTAMPTZ NOT NULL,
    seeders           INT,
    leechers          INT,
    last_scraped      TIMESTAMPTZ,
    uploader          TEXT,
    downloads         INT,
    tags              TEXT[],
    magnet            TEXT NOT NULL,
    image_url         TEXT,
    images            TEXT[],
    description_text  TEXT,
    media_info        JSONB,
    files             JSONB,
    likes             INT,
    dislikes          INT,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    scraped_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_refresh_at   TIMESTAMPTZ,
    CONSTRAINT info_hash_shape CHECK (info_hash IS NULL OR info_hash ~ '^[a-f0-9]{40}$')
);
CREATE INDEX IF NOT EXISTS idx_torrents_info_hash ON torrents (info_hash);
CREATE INDEX IF NOT EXISTS idx_torrents_next_refresh_at ON torrents (next_refresh_at);
CREATE INDEX IF NOT EXISTS idx_torrents_added_at ON torrents (added_at);

CREATE TABLE IF NOT EXISTS id_status (
    torrent_id        BIGINT PRIMARY KEY,
    status            TEXT NOT NULL CHECK (status IN (
                          'frontier_missing', 'internal_gap', 'gone',
                          'transient_error', 'parse_error'
                      )),
    attempts          INT NOT NULL DEFAULT 1,
    last_checked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at     TIMESTAMPTZ,
    last_http_status  TEXT
);
CREATE INDEX IF NOT EXISTS idx_id_status_next_retry_at ON id_status (next_retry_at);
CREATE INDEX IF NOT EXISTS idx_id_status_status ON id_status (status);

CREATE TABLE IF NOT EXISTS crawl_state (
    id                          INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    highest_success_id          BIGINT NOT NULL DEFAULT 0,
    contiguous_floor_id         BIGINT,
    backfill_cutoff_at          TIMESTAMPTZ,
    backfill_completed_at       TIMESTAMPTZ,
    frontier_scan_high          BIGINT NOT NULL DEFAULT 0,
    backfill_confirm_anchor_id  BIGINT
);
INSERT INTO crawl_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""

TORRENT_COLUMNS = [
    "torrent_id", "info_hash", "title", "category", "size_bytes",
    "added_at", "seeders", "leechers", "last_scraped",
    "uploader", "downloads", "tags", "magnet", "image_url", "images",
    "description_text", "media_info", "files", "likes", "dislikes", "next_refresh_at",
]


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10)


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def upsert_torrent(pool: asyncpg.Pool, data: dict) -> None:
    cols = TORRENT_COLUMNS
    placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
    update_cols = [c for c in cols if c != "torrent_id"]
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    sql = f"""
        INSERT INTO torrents ({", ".join(cols)}, scraped_at)
        VALUES ({placeholders}, now())
        ON CONFLICT (torrent_id) DO UPDATE SET
            {update_set},
            scraped_at = now()
    """
    values = []
    for c in cols:
        v = data.get(c)
        if c in JSONB_COLUMNS and v is not None:
            v = json.dumps(v)
        values.append(v)
    async with pool.acquire() as conn:
        await conn.execute(sql, *values)
        await conn.execute(
            "DELETE FROM id_status WHERE torrent_id = $1", data["torrent_id"]
        )


async def upsert_id_status(
    pool: asyncpg.Pool,
    torrent_id: int,
    status: str,
    last_http_status: str | None,
    next_retry_at,
) -> None:
    sql = """
        INSERT INTO id_status (torrent_id, status, attempts, last_checked_at, next_retry_at, last_http_status)
        VALUES ($1, $2, 1, now(), $3, $4)
        ON CONFLICT (torrent_id) DO UPDATE SET
            status = EXCLUDED.status,
            attempts = id_status.attempts + 1,
            last_checked_at = now(),
            next_retry_at = EXCLUDED.next_retry_at,
            last_http_status = EXCLUDED.last_http_status
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, torrent_id, status, next_retry_at, last_http_status)


async def reclassify_internal_gaps(pool: asyncpg.Pool, highest_success_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE id_status SET status = 'internal_gap'
            WHERE status = 'frontier_missing' AND torrent_id < $1
            """,
            highest_success_id,
        )


async def get_crawl_state(pool: asyncpg.Pool) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM crawl_state WHERE id = 1")


async def update_crawl_state(pool: asyncpg.Pool, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ${i+1}" for i, k in enumerate(fields))
    sql = f"UPDATE crawl_state SET {set_clause} WHERE id = 1"
    async with pool.acquire() as conn:
        await conn.execute(sql, *fields.values())


async def fetch_retry_due(pool: asyncpg.Pool, limit: int) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT torrent_id, status FROM id_status
            WHERE next_retry_at IS NOT NULL AND next_retry_at <= now()
            ORDER BY next_retry_at
            LIMIT $1
            """,
            limit,
        )


async def fetch_refresh_due(pool: asyncpg.Pool, limit: int) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT torrent_id FROM torrents
            WHERE next_refresh_at IS NOT NULL AND next_refresh_at <= now()
            ORDER BY next_refresh_at
            LIMIT $1
            """,
            limit,
        )


async def bump_next_refresh(pool: asyncpg.Pool, torrent_id: int, next_refresh_at) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE torrents SET next_refresh_at = $2 WHERE torrent_id = $1",
            torrent_id,
            next_refresh_at,
        )


def _build_where(q: str | None, category: str | None, tag: str | None) -> tuple[str, list]:
    clauses = []
    params: list = []
    if q:
        params.append(f"%{q}%")
        clauses.append(f"(title ILIKE ${len(params)} OR uploader ILIKE ${len(params)})")
    if category:
        params.append(category)
        clauses.append(f"category = ${len(params)}")
    if tag:
        params.append(tag)
        clauses.append(f"${len(params)} = ANY(tags)")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


async def count_torrents(
    pool: asyncpg.Pool, *, q: str | None = None, category: str | None = None, tag: str | None = None
) -> int:
    where, params = _build_where(q, category, tag)
    async with pool.acquire() as conn:
        return await conn.fetchval(f"SELECT count(*) FROM torrents {where}", *params)


SORT_OPTIONS = {
    "added_desc": "added_at DESC NULLS LAST",
    "added_asc": "added_at ASC NULLS LAST",
    "seeders_desc": "seeders DESC NULLS LAST",
    "size_desc": "size_bytes DESC NULLS LAST",
    "downloads_desc": "downloads DESC NULLS LAST",
}
DEFAULT_SORT = "added_desc"


async def list_torrents(
    pool: asyncpg.Pool,
    *,
    q: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    sort: str = DEFAULT_SORT,
    limit: int = 24,
    offset: int = 0,
) -> list[asyncpg.Record]:
    where, params = _build_where(q, category, tag)
    order_by = SORT_OPTIONS.get(sort, SORT_OPTIONS[DEFAULT_SORT])
    params.append(limit)
    limit_idx = len(params)
    params.append(offset)
    offset_idx = len(params)
    sql = f"""
        SELECT torrent_id, title, category, size_bytes, added_at, seeders, leechers,
               uploader, downloads, tags, image_url, magnet
        FROM torrents
        {where}
        ORDER BY {order_by}
        LIMIT ${limit_idx} OFFSET ${offset_idx}
    """
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *params)


async def get_torrent(pool: asyncpg.Pool, torrent_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM torrents WHERE torrent_id = $1", torrent_id)
    if row is None:
        return None
    data = dict(row)
    for col in JSONB_COLUMNS:
        if isinstance(data.get(col), str):
            data[col] = json.loads(data[col])
    return data


async def distinct_categories(pool: asyncpg.Pool, limit: int = 12) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT category, count(*) AS n FROM torrents
            WHERE category IS NOT NULL
            GROUP BY category ORDER BY n DESC LIMIT $1
            """,
            limit,
        )


async def top_tags(pool: asyncpg.Pool, limit: int | None = 12) -> list[asyncpg.Record]:
    sql = "SELECT tag, count(*) AS n FROM torrents, unnest(tags) AS tag GROUP BY tag ORDER BY n DESC"
    async with pool.acquire() as conn:
        if limit is None:
            return await conn.fetch(sql)
        return await conn.fetch(f"{sql} LIMIT $1", limit)


async def confirm_window_all_old(
    pool: asyncpg.Pool, floor_id: int, window: int, cutoff_at
) -> tuple[bool, int]:
    """Among torrents rows in [floor_id, floor_id+window-1], are all dated ones older than cutoff?
    Returns (all_old_and_at_least_one_dated, dated_count)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE added_at IS NOT NULL) AS dated_count,
                bool_and(added_at < $3) FILTER (WHERE added_at IS NOT NULL) AS all_old
            FROM torrents
            WHERE torrent_id BETWEEN $1 AND $1 + $2 - 1
            """,
            floor_id,
            window,
            cutoff_at,
        )
    dated_count = row["dated_count"] or 0
    all_old = row["all_old"]
    return bool(dated_count > 0 and all_old), dated_count
