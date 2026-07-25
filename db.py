"""Postgres schema + data-access helpers. See PLAN.md Data model / Non-negotiables 1+2."""

import json

import asyncpg

JSONB_COLUMNS = {"files", "media_info"}
TPDB_CATEGORIES = (
    "1080p/FullHD",
    "2160p/UHD/4K",
    "480p/SD",
    "720p/HD",
    "VR/VirtualReality",
)

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

CREATE TABLE IF NOT EXISTS tpdb_networks (
    network_id        BIGINT PRIMARY KEY,
    uuid              UUID UNIQUE,
    name              TEXT NOT NULL,
    short_name        TEXT,
    url               TEXT,
    description       TEXT,
    rating            NUMERIC,
    logo_url          TEXT,
    favicon_url       TEXT,
    poster_url        TEXT,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tpdb_networks_name ON tpdb_networks (name);

CREATE TABLE IF NOT EXISTS tpdb_sites (
    site_id           BIGINT PRIMARY KEY,
    uuid              UUID UNIQUE,
    network_id        BIGINT REFERENCES tpdb_networks(network_id) ON DELETE SET NULL,
    parent_id         BIGINT,
    name              TEXT NOT NULL,
    short_name        TEXT,
    url               TEXT,
    description       TEXT,
    rating            NUMERIC,
    logo_url          TEXT,
    favicon_url       TEXT,
    poster_url        TEXT,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tpdb_sites_network_id ON tpdb_sites (network_id);
CREATE INDEX IF NOT EXISTS idx_tpdb_sites_name ON tpdb_sites (name);

CREATE TABLE IF NOT EXISTS tpdb_scenes (
    scene_id          UUID PRIMARY KEY,
    tpdb_id           BIGINT UNIQUE NOT NULL,
    site_id           BIGINT REFERENCES tpdb_sites(site_id) ON DELETE SET NULL,
    title             TEXT NOT NULL,
    type              TEXT,
    slug              TEXT,
    external_id       TEXT,
    description       TEXT,
    rating            NUMERIC,
    release_date      DATE,
    url               TEXT,
    image_url         TEXT,
    back_image_url    TEXT,
    poster_url        TEXT,
    background_url    TEXT,
    trailer_url       TEXT,
    duration_seconds  INT,
    format            TEXT,
    sku               TEXT,
    tags              TEXT[],
    backgrounds       JSONB,
    hashes            JSONB,
    directors         JSONB,
    links             JSONB,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE tpdb_scenes ADD COLUMN IF NOT EXISTS background_url TEXT;
UPDATE tpdb_scenes
SET background_url = COALESCE(
    backgrounds->'front'->>'full',
    backgrounds->'front'->>'large',
    metadata->'background'->>'full',
    metadata->'background'->>'large',
    metadata->>'image'
)
WHERE background_url IS NULL;
CREATE INDEX IF NOT EXISTS idx_tpdb_scenes_site_id ON tpdb_scenes (site_id);
CREATE INDEX IF NOT EXISTS idx_tpdb_scenes_release_date ON tpdb_scenes (release_date DESC);
CREATE INDEX IF NOT EXISTS idx_tpdb_scenes_title ON tpdb_scenes (title);
CREATE INDEX IF NOT EXISTS idx_tpdb_scenes_tags ON tpdb_scenes USING GIN (tags);

CREATE TABLE IF NOT EXISTS tpdb_performers (
    performer_id      UUID PRIMARY KEY,
    tpdb_id           BIGINT UNIQUE,
    name              TEXT NOT NULL,
    slug              TEXT,
    full_name         TEXT,
    disambiguation    TEXT,
    bio               TEXT,
    rating            NUMERIC,
    gender            TEXT,
    birth_date        DATE,
    birthplace        TEXT,
    nationality       TEXT,
    ethnicity         TEXT,
    hair_colour       TEXT,
    eye_colour        TEXT,
    height            TEXT,
    weight            TEXT,
    measurements      TEXT,
    cupsize           TEXT,
    tattoos           TEXT,
    piercings         TEXT,
    image_url         TEXT,
    thumbnail_url     TEXT,
    face_url          TEXT,
    extras            JSONB,
    posters           JSONB,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tpdb_performers_name ON tpdb_performers (name);

CREATE TABLE IF NOT EXISTS tpdb_scene_performers (
    scene_id          UUID NOT NULL REFERENCES tpdb_scenes(scene_id) ON DELETE CASCADE,
    performer_id      UUID NOT NULL REFERENCES tpdb_performers(performer_id) ON DELETE CASCADE,
    billing_order     INT NOT NULL DEFAULT 0,
    PRIMARY KEY (scene_id, performer_id)
);
CREATE INDEX IF NOT EXISTS idx_tpdb_scene_performers_performer
    ON tpdb_scene_performers (performer_id);

CREATE TABLE IF NOT EXISTS tpdb_match_attempts (
    torrent_id        BIGINT PRIMARY KEY REFERENCES torrents(torrent_id) ON DELETE CASCADE,
    scene_id          UUID REFERENCES tpdb_scenes(scene_id) ON DELETE SET NULL,
    filename          TEXT,
    file_size_bytes   BIGINT,
    method            TEXT NOT NULL DEFAULT 'parse_filename_first',
    scene_key         TEXT,
    search_query      TEXT,
    match_score       NUMERIC,
    candidate_metadata JSONB NOT NULL DEFAULT '[]'::jsonb,
    status            TEXT NOT NULL CHECK (status IN ('matched', 'unmatched', 'error', 'no_file')),
    candidate_count   INT NOT NULL DEFAULT 0,
    attempts          INT NOT NULL DEFAULT 1,
    http_status       INT,
    last_error        TEXT,
    attempted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_retry_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tpdb_match_attempts_scene_id
    ON tpdb_match_attempts (scene_id);
CREATE INDEX IF NOT EXISTS idx_tpdb_match_attempts_status
    ON tpdb_match_attempts (status);
ALTER TABLE tpdb_match_attempts ADD COLUMN IF NOT EXISTS scene_key TEXT;
ALTER TABLE tpdb_match_attempts ADD COLUMN IF NOT EXISTS search_query TEXT;
ALTER TABLE tpdb_match_attempts ADD COLUMN IF NOT EXISTS match_score NUMERIC;
ALTER TABLE tpdb_match_attempts
    ADD COLUMN IF NOT EXISTS candidate_metadata JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS idx_tpdb_match_attempts_scene_key
    ON tpdb_match_attempts (scene_key) WHERE status = 'matched';
UPDATE tpdb_match_attempts
SET scene_key = btrim(
    regexp_replace(
        regexp_replace(
            regexp_replace(
                lower(filename),
                '\\.(mp4|mkv|avi|wmv|mov|m4v|ts)$',
                '',
                'i'
            ),
            '([._ -](480p|720p|1080p|2160p|4k|uhd|fhd|fullhd|x264|x265|h264|h265|hevc))+$',
            '',
            'i'
        ),
        '[^a-z0-9]+',
        '.',
        'g'
    ),
    '.'
)
WHERE scene_key IS NULL AND filename IS NOT NULL;
UPDATE tpdb_match_attempts
SET next_retry_at = now()
WHERE status = 'unmatched' AND next_retry_at IS NULL;
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
        async with conn.transaction():
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


async def max_torrent_id(pool: asyncpg.Pool) -> int:
    """Highest successfully-stored torrent_id (PK scan, cheap). 0 when empty.

    Used by the frontier to bound how far it probes ahead: successes found by the
    retry ledger (not just frontier_cycle) are reflected here, so the frontier cap
    tracks real growth without a per-request crawl_state write."""
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COALESCE(max(torrent_id), 0) FROM torrents")


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


async def fetch_tpdb_match_candidates(
    pool: asyncpg.Pool, limit: int
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT t.torrent_id, t.title, t.category, t.files,
                   COALESCE(a.attempts, 0) AS attempts
            FROM torrents t
            LEFT JOIN tpdb_match_attempts a ON a.torrent_id = t.torrent_id
            WHERE t.category = ANY($1::text[])
              AND (
                  a.torrent_id IS NULL
                  OR (
                      a.status IN ('error', 'unmatched')
                      AND a.next_retry_at IS NOT NULL
                      AND a.next_retry_at <= now()
                  )
              )
            ORDER BY t.added_at DESC, t.torrent_id DESC
            LIMIT $2
            """,
            list(TPDB_CATEGORIES),
            limit,
        )


async def fetch_tpdb_shadow_candidates(
    pool: asyncpg.Pool, limit: int
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT t.torrent_id, t.title, t.category, t.files,
                   COALESCE(a.attempts, 0) AS attempts
            FROM torrents t
            LEFT JOIN tpdb_match_attempts a ON a.torrent_id = t.torrent_id
            WHERE t.category = ANY($1::text[])
              AND (a.torrent_id IS NULL OR a.status = 'unmatched')
            ORDER BY t.added_at DESC, t.torrent_id DESC
            LIMIT $2
            """,
            list(TPDB_CATEGORIES),
            limit,
        )


async def record_tpdb_match_outcome(
    pool: asyncpg.Pool,
    *,
    torrent_id: int,
    filename: str | None,
    file_size_bytes: int | None,
    status: str,
    candidate_count: int = 0,
    http_status: int | None = None,
    last_error: str | None = None,
    next_retry_at=None,
    method: str = "parse_filename",
    scene_key: str | None = None,
    search_query: str | None = None,
    match_score: float | None = None,
    candidate_metadata: list | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tpdb_match_attempts (
                torrent_id, filename, file_size_bytes, status, candidate_count,
                http_status, last_error, attempted_at, next_retry_at, method,
                scene_key, search_query, match_score, candidate_metadata
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, now(), $8, $9, $10, $11,
                $12, $13::jsonb
            )
            ON CONFLICT (torrent_id) DO UPDATE SET
                scene_id = NULL,
                filename = EXCLUDED.filename,
                file_size_bytes = EXCLUDED.file_size_bytes,
                status = EXCLUDED.status,
                candidate_count = EXCLUDED.candidate_count,
                attempts = tpdb_match_attempts.attempts + 1,
                http_status = EXCLUDED.http_status,
                last_error = EXCLUDED.last_error,
                attempted_at = now(),
                next_retry_at = EXCLUDED.next_retry_at,
                method = EXCLUDED.method,
                scene_key = EXCLUDED.scene_key,
                search_query = EXCLUDED.search_query,
                match_score = EXCLUDED.match_score,
                candidate_metadata = EXCLUDED.candidate_metadata
            """,
            torrent_id,
            filename,
            file_size_bytes,
            status,
            candidate_count,
            http_status,
            last_error,
            next_retry_at,
            method,
            scene_key,
            search_query,
            match_score,
            json.dumps(candidate_metadata or []),
        )


async def find_reusable_tpdb_match(
    pool: asyncpg.Pool,
    scene_key: str,
    torrent_id: int,
) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT a.scene_id, a.torrent_id AS source_torrent_id
            FROM tpdb_match_attempts a
            WHERE a.status = 'matched'
              AND a.scene_id IS NOT NULL
              AND a.scene_key = $1
              AND a.torrent_id <> $2
            ORDER BY a.attempted_at DESC
            LIMIT 1
            """,
            scene_key,
            torrent_id,
        )


async def save_reused_tpdb_match(
    pool: asyncpg.Pool,
    *,
    torrent_id: int,
    scene_id,
    filename: str,
    file_size_bytes: int | None,
    scene_key: str,
    source_torrent_id: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tpdb_match_attempts (
                torrent_id, scene_id, filename, file_size_bytes, method,
                scene_key, search_query, match_score, candidate_metadata,
                status, candidate_count, http_status, last_error, attempted_at,
                next_retry_at
            )
            VALUES (
                $1,$2,$3,$4,'resolution_sibling',$5,NULL,1,
                $6::jsonb,'matched',1,200,NULL,now(),NULL
            )
            ON CONFLICT (torrent_id) DO UPDATE SET
                scene_id=EXCLUDED.scene_id,
                filename=EXCLUDED.filename,
                file_size_bytes=EXCLUDED.file_size_bytes,
                method=EXCLUDED.method,
                scene_key=EXCLUDED.scene_key,
                search_query=NULL,
                match_score=1,
                candidate_metadata=EXCLUDED.candidate_metadata,
                status='matched',
                candidate_count=1,
                attempts=tpdb_match_attempts.attempts + 1,
                http_status=200,
                last_error=NULL,
                attempted_at=now(),
                next_retry_at=NULL
            """,
            torrent_id,
            scene_id,
            filename,
            file_size_bytes,
            scene_key,
            json.dumps([{"source_torrent_id": source_torrent_id}]),
        )


async def save_tpdb_match(
    pool: asyncpg.Pool,
    *,
    torrent_id: int,
    filename: str,
    file_size_bytes: int | None,
    candidate_count: int,
    bundle: dict,
    method: str = "parse_filename",
    scene_key: str | None = None,
    search_query: str | None = None,
    match_score: float | None = None,
    candidate_metadata: list | None = None,
) -> None:
    scene = bundle["scene"]
    site = bundle.get("site")
    network = bundle.get("network")
    performers = bundle.get("performers", [])

    async with pool.acquire() as conn:
        async with conn.transaction():
            if network:
                await conn.execute(
                    """
                    INSERT INTO tpdb_networks (
                        network_id, uuid, name, short_name, url, description,
                        rating, logo_url, favicon_url, poster_url, metadata, updated_at
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,now())
                    ON CONFLICT (network_id) DO UPDATE SET
                        uuid=EXCLUDED.uuid, name=EXCLUDED.name,
                        short_name=EXCLUDED.short_name, url=EXCLUDED.url,
                        description=EXCLUDED.description, rating=EXCLUDED.rating,
                        logo_url=EXCLUDED.logo_url, favicon_url=EXCLUDED.favicon_url,
                        poster_url=EXCLUDED.poster_url, metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """,
                    network["network_id"],
                    network.get("uuid"),
                    network["name"],
                    network.get("short_name"),
                    network.get("url"),
                    network.get("description"),
                    network.get("rating"),
                    network.get("logo_url"),
                    network.get("favicon_url"),
                    network.get("poster_url"),
                    json.dumps(network.get("metadata") or {}),
                )

            if site:
                await conn.execute(
                    """
                    INSERT INTO tpdb_sites (
                        site_id, uuid, network_id, parent_id, name, short_name,
                        url, description, rating, logo_url, favicon_url,
                        poster_url, metadata, updated_at
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,now())
                    ON CONFLICT (site_id) DO UPDATE SET
                        uuid=EXCLUDED.uuid, network_id=EXCLUDED.network_id,
                        parent_id=EXCLUDED.parent_id, name=EXCLUDED.name,
                        short_name=EXCLUDED.short_name, url=EXCLUDED.url,
                        description=EXCLUDED.description, rating=EXCLUDED.rating,
                        logo_url=EXCLUDED.logo_url, favicon_url=EXCLUDED.favicon_url,
                        poster_url=EXCLUDED.poster_url, metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """,
                    site["site_id"],
                    site.get("uuid"),
                    network["network_id"] if network else None,
                    site.get("parent_id"),
                    site["name"],
                    site.get("short_name"),
                    site.get("url"),
                    site.get("description"),
                    site.get("rating"),
                    site.get("logo_url"),
                    site.get("favicon_url"),
                    site.get("poster_url"),
                    json.dumps(site.get("metadata") or {}),
                )

            await conn.execute(
                """
                INSERT INTO tpdb_scenes (
                    scene_id, tpdb_id, site_id, title, type, slug, external_id,
                    description, rating, release_date, url, image_url,
                    back_image_url, poster_url, background_url, trailer_url, duration_seconds,
                    format, sku, tags, backgrounds, hashes, directors, links,
                    metadata, updated_at
                )
                VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                    $17,$18,$19,$20,$21::jsonb,$22::jsonb,$23::jsonb,$24::jsonb,
                    $25::jsonb,now()
                )
                ON CONFLICT (scene_id) DO UPDATE SET
                    tpdb_id=EXCLUDED.tpdb_id, site_id=EXCLUDED.site_id,
                    title=EXCLUDED.title, type=EXCLUDED.type, slug=EXCLUDED.slug,
                    external_id=EXCLUDED.external_id,
                    description=EXCLUDED.description, rating=EXCLUDED.rating,
                    release_date=EXCLUDED.release_date, url=EXCLUDED.url,
                    image_url=EXCLUDED.image_url, back_image_url=EXCLUDED.back_image_url,
                    poster_url=EXCLUDED.poster_url,
                    background_url=EXCLUDED.background_url,
                    trailer_url=EXCLUDED.trailer_url,
                    duration_seconds=EXCLUDED.duration_seconds,
                    format=EXCLUDED.format, sku=EXCLUDED.sku, tags=EXCLUDED.tags,
                    backgrounds=EXCLUDED.backgrounds, hashes=EXCLUDED.hashes,
                    directors=EXCLUDED.directors, links=EXCLUDED.links,
                    metadata=EXCLUDED.metadata, updated_at=now()
                """,
                scene["scene_id"],
                scene["tpdb_id"],
                site["site_id"] if site else None,
                scene["title"],
                scene.get("type"),
                scene.get("slug"),
                scene.get("external_id"),
                scene.get("description"),
                scene.get("rating"),
                scene.get("release_date"),
                scene.get("url"),
                scene.get("image_url"),
                scene.get("back_image_url"),
                scene.get("poster_url"),
                scene.get("background_url"),
                scene.get("trailer_url"),
                scene.get("duration_seconds"),
                scene.get("format"),
                scene.get("sku"),
                scene.get("tags"),
                json.dumps(scene.get("backgrounds")),
                json.dumps(scene.get("hashes")),
                json.dumps(scene.get("directors")),
                json.dumps(scene.get("links")),
                json.dumps(scene.get("metadata") or {}),
            )

            await conn.execute(
                "DELETE FROM tpdb_scene_performers WHERE scene_id = $1",
                scene["scene_id"],
            )
            for order, performer in enumerate(performers):
                await conn.execute(
                    """
                    INSERT INTO tpdb_performers (
                        performer_id, tpdb_id, name, slug, full_name,
                        disambiguation, bio, rating, gender, birth_date,
                        birthplace, nationality, ethnicity, hair_colour,
                        eye_colour, height, weight, measurements, cupsize,
                        tattoos, piercings, image_url, thumbnail_url, face_url,
                        extras, posters, metadata, updated_at
                    )
                    VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                        $15,$16,$17,$18,$19,$20,$21,$22,$23,$24,
                        $25::jsonb,$26::jsonb,$27::jsonb,now()
                    )
                    ON CONFLICT (performer_id) DO UPDATE SET
                        tpdb_id=EXCLUDED.tpdb_id, name=EXCLUDED.name,
                        slug=EXCLUDED.slug, full_name=EXCLUDED.full_name,
                        disambiguation=EXCLUDED.disambiguation, bio=EXCLUDED.bio,
                        rating=EXCLUDED.rating, gender=EXCLUDED.gender,
                        birth_date=EXCLUDED.birth_date,
                        birthplace=EXCLUDED.birthplace,
                        nationality=EXCLUDED.nationality,
                        ethnicity=EXCLUDED.ethnicity,
                        hair_colour=EXCLUDED.hair_colour,
                        eye_colour=EXCLUDED.eye_colour,
                        height=EXCLUDED.height, weight=EXCLUDED.weight,
                        measurements=EXCLUDED.measurements,
                        cupsize=EXCLUDED.cupsize, tattoos=EXCLUDED.tattoos,
                        piercings=EXCLUDED.piercings,
                        image_url=EXCLUDED.image_url,
                        thumbnail_url=EXCLUDED.thumbnail_url,
                        face_url=EXCLUDED.face_url, extras=EXCLUDED.extras,
                        posters=EXCLUDED.posters, metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """,
                    performer["performer_id"],
                    performer.get("tpdb_id"),
                    performer["name"],
                    performer.get("slug"),
                    performer.get("full_name"),
                    performer.get("disambiguation"),
                    performer.get("bio"),
                    performer.get("rating"),
                    performer.get("gender"),
                    performer.get("birth_date"),
                    performer.get("birthplace"),
                    performer.get("nationality"),
                    performer.get("ethnicity"),
                    performer.get("hair_colour"),
                    performer.get("eye_colour"),
                    performer.get("height"),
                    performer.get("weight"),
                    performer.get("measurements"),
                    performer.get("cupsize"),
                    performer.get("tattoos"),
                    performer.get("piercings"),
                    performer.get("image_url"),
                    performer.get("thumbnail_url"),
                    performer.get("face_url"),
                    json.dumps(performer.get("extras")),
                    json.dumps(performer.get("posters")),
                    json.dumps(performer.get("metadata") or {}),
                )
                await conn.execute(
                    """
                    INSERT INTO tpdb_scene_performers (
                        scene_id, performer_id, billing_order
                    )
                    VALUES ($1, $2, $3)
                    ON CONFLICT (scene_id, performer_id) DO UPDATE SET
                        billing_order=EXCLUDED.billing_order
                    """,
                    scene["scene_id"],
                    performer["performer_id"],
                    order,
                )

            await conn.execute(
                """
                INSERT INTO tpdb_match_attempts (
                    torrent_id, scene_id, filename, file_size_bytes, status,
                    candidate_count, http_status, last_error, attempted_at,
                    next_retry_at, method, scene_key, search_query, match_score,
                    candidate_metadata
                )
                VALUES (
                    $1,$2,$3,$4,'matched',$5,200,NULL,now(),NULL,$6,$7,$8,$9,
                    $10::jsonb
                )
                ON CONFLICT (torrent_id) DO UPDATE SET
                    scene_id=EXCLUDED.scene_id, filename=EXCLUDED.filename,
                    file_size_bytes=EXCLUDED.file_size_bytes,
                    status='matched',
                    candidate_count=EXCLUDED.candidate_count,
                    attempts=tpdb_match_attempts.attempts + 1,
                    http_status=200, last_error=NULL, attempted_at=now(),
                    next_retry_at=NULL, method=EXCLUDED.method,
                    scene_key=EXCLUDED.scene_key,
                    search_query=EXCLUDED.search_query,
                    match_score=EXCLUDED.match_score,
                    candidate_metadata=EXCLUDED.candidate_metadata
                """,
                torrent_id,
                scene["scene_id"],
                filename,
                file_size_bytes,
                candidate_count,
                method,
                scene_key,
                search_query,
                match_score,
                json.dumps(candidate_metadata or []),
            )


async def get_tpdb_match_stats(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (
                    WHERE t.category = ANY($1::text[])
                ) AS eligible,
                count(a.torrent_id) FILTER (
                    WHERE t.category = ANY($1::text[])
                ) AS attempted,
                count(*) FILTER (
                    WHERE t.category = ANY($1::text[]) AND a.status = 'matched'
                ) AS matched,
                count(*) FILTER (
                    WHERE t.category = ANY($1::text[]) AND a.status = 'unmatched'
                ) AS unmatched,
                count(*) FILTER (
                    WHERE t.category = ANY($1::text[]) AND a.status = 'error'
                ) AS errors,
                count(*) FILTER (
                    WHERE t.category = ANY($1::text[]) AND a.status = 'no_file'
                ) AS no_file
            FROM torrents t
            LEFT JOIN tpdb_match_attempts a ON a.torrent_id = t.torrent_id
            """,
            list(TPDB_CATEGORIES),
        )
    stats = dict(row)
    stats["pending"] = max(0, stats["eligible"] - stats["attempted"])
    stats["match_rate"] = (
        stats["matched"] / stats["attempted"] * 100 if stats["attempted"] else 0.0
    )
    return stats


CATALOG_ENTITY_CONFIG = {
    "scenes": {
        "count": """
            SELECT count(*)
            FROM tpdb_scenes
            WHERE ($1::text IS NULL OR title ILIKE $1)
              AND ($2::text IS NULL OR tags @> ARRAY[$2]::text[])
        """,
        "list": """
            SELECT s.scene_id AS id, s.title AS name, s.release_date AS date,
                   COALESCE(s.background_url, s.image_url, s.poster_url) AS image_url,
                   st.name AS secondary, count(DISTINCT sp.performer_id) AS related_count
            FROM tpdb_scenes s
            LEFT JOIN tpdb_sites st ON st.site_id = s.site_id
            LEFT JOIN tpdb_scene_performers sp ON sp.scene_id = s.scene_id
            WHERE ($1::text IS NULL OR s.title ILIKE $1)
              AND ($2::text IS NULL OR s.tags @> ARRAY[$2]::text[])
            GROUP BY s.scene_id, st.name
            ORDER BY s.release_date DESC NULLS LAST, s.title
            LIMIT $3 OFFSET $4
        """,
    },
    "sites": {
        "count": "SELECT count(*) FROM tpdb_sites WHERE ($1::text IS NULL OR name ILIKE $1)",
        "list": """
            SELECT st.site_id AS id, st.name, NULL::date AS date,
                   COALESCE(st.poster_url, st.logo_url) AS image_url,
                   n.name AS secondary, count(DISTINCT s.scene_id) AS related_count
            FROM tpdb_sites st
            LEFT JOIN tpdb_networks n ON n.network_id = st.network_id
            LEFT JOIN tpdb_scenes s ON s.site_id = st.site_id
            WHERE ($1::text IS NULL OR st.name ILIKE $1)
            GROUP BY st.site_id, n.name
            ORDER BY st.name
            LIMIT $2 OFFSET $3
        """,
    },
    "networks": {
        "count": "SELECT count(*) FROM tpdb_networks WHERE ($1::text IS NULL OR name ILIKE $1)",
        "list": """
            SELECT n.network_id AS id, n.name, NULL::date AS date,
                   COALESCE(n.poster_url, n.logo_url) AS image_url,
                   NULL::text AS secondary, count(DISTINCT st.site_id) AS related_count
            FROM tpdb_networks n
            LEFT JOIN tpdb_sites st ON st.network_id = n.network_id
            WHERE ($1::text IS NULL OR n.name ILIKE $1)
            GROUP BY n.network_id
            ORDER BY n.name
            LIMIT $2 OFFSET $3
        """,
    },
    "performers": {
        "count": "SELECT count(*) FROM tpdb_performers WHERE ($1::text IS NULL OR name ILIKE $1)",
        "list": """
            SELECT p.performer_id AS id, p.name, p.birth_date AS date,
                   COALESCE(p.image_url, p.face_url, p.thumbnail_url) AS image_url,
                   p.nationality AS secondary,
                   count(DISTINCT sp.scene_id) AS related_count
            FROM tpdb_performers p
            LEFT JOIN tpdb_scene_performers sp ON sp.performer_id = p.performer_id
            WHERE ($1::text IS NULL OR p.name ILIKE $1)
            GROUP BY p.performer_id
            ORDER BY p.name
            LIMIT $2 OFFSET $3
        """,
    },
}


async def count_catalog_entities(
    pool: asyncpg.Pool,
    entity: str,
    q: str | None = None,
    tag: str | None = None,
) -> int:
    config = CATALOG_ENTITY_CONFIG[entity]
    search = f"%{q}%" if q else None
    async with pool.acquire() as conn:
        if entity == "scenes":
            return await conn.fetchval(config["count"], search, tag)
        return await conn.fetchval(config["count"], search)


async def list_catalog_entities(
    pool: asyncpg.Pool,
    entity: str,
    *,
    q: str | None = None,
    tag: str | None = None,
    limit: int = 24,
    offset: int = 0,
) -> list[asyncpg.Record]:
    config = CATALOG_ENTITY_CONFIG[entity]
    search = f"%{q}%" if q else None
    async with pool.acquire() as conn:
        if entity == "scenes":
            return await conn.fetch(config["list"], search, tag, limit, offset)
        return await conn.fetch(config["list"], search, limit, offset)


async def top_scene_tags(
    pool: asyncpg.Pool, limit: int = 16
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT tag, count(*) AS n
            FROM tpdb_scenes
            CROSS JOIN LATERAL unnest(tags) AS tag
            WHERE btrim(tag) <> ''
            GROUP BY tag
            ORDER BY n DESC, tag
            LIMIT $1
            """,
            limit,
        )


def _performer_image_urls(detail: dict) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            images.append(value)

    def add_profile(profile) -> None:
        if not isinstance(profile, dict):
            return
        add(profile.get("image"))
        add(profile.get("face"))
        add(profile.get("thumbnail"))
        for poster in profile.get("posters") or []:
            if isinstance(poster, dict):
                add(poster.get("url"))
            else:
                add(poster)

    add(detail.get("image_url"))
    add(detail.get("face_url"))
    add(detail.get("thumbnail_url"))
    posters = detail.get("posters")
    if isinstance(posters, str):
        try:
            posters = json.loads(posters)
        except json.JSONDecodeError:
            posters = []
    for poster in posters or []:
        if isinstance(poster, dict):
            add(poster.get("url"))
        else:
            add(poster)

    metadata = detail.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    if isinstance(metadata, dict):
        add_profile(metadata.get("profile"))
        add_profile(metadata.get("site_profile"))
    return images


def _torrent_image_urls(torrents: list[dict]) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for torrent in torrents:
        candidates = [torrent.get("image_url"), *(torrent.get("images") or [])]
        for value in candidates:
            if isinstance(value, str) and value and value not in seen:
                seen.add(value)
                images.append(value)
    return images


async def get_catalog_detail(pool: asyncpg.Pool, entity: str, entity_id) -> dict | None:
    async with pool.acquire() as conn:
        if entity == "scenes":
            row = await conn.fetchrow(
                """
                SELECT s.*, st.name AS site_name, st.network_id,
                       n.name AS network_name
                FROM tpdb_scenes s
                LEFT JOIN tpdb_sites st ON st.site_id = s.site_id
                LEFT JOIN tpdb_networks n ON n.network_id = st.network_id
                WHERE s.scene_id = $1
                """,
                entity_id,
            )
            if row is None:
                return None
            detail = dict(row)
            detail["performers"] = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT p.performer_id AS id, p.name,
                           COALESCE(p.face_url, p.thumbnail_url, p.image_url) AS image_url
                    FROM tpdb_performers p
                    JOIN tpdb_scene_performers sp
                      ON sp.performer_id = p.performer_id
                    WHERE sp.scene_id = $1
                    ORDER BY sp.billing_order
                    """,
                    entity_id,
                )
            ]
            detail["torrents"] = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT t.torrent_id, t.title, t.category, t.size_bytes,
                           t.magnet, t.seeders, t.leechers, t.image_url,
                           t.images, a.filename
                    FROM tpdb_match_attempts a
                    JOIN torrents t ON t.torrent_id = a.torrent_id
                    WHERE a.scene_id = $1
                    ORDER BY t.added_at DESC
                    """,
                    entity_id,
                )
            ]
            detail["torrent_images"] = _torrent_image_urls(detail["torrents"])
            return detail

        if entity == "sites":
            row = await conn.fetchrow(
                """
                SELECT st.*, n.name AS network_name
                FROM tpdb_sites st
                LEFT JOIN tpdb_networks n ON n.network_id = st.network_id
                WHERE st.site_id = $1
                """,
                entity_id,
            )
        elif entity == "networks":
            row = await conn.fetchrow(
                "SELECT * FROM tpdb_networks WHERE network_id = $1", entity_id
            )
        elif entity == "performers":
            row = await conn.fetchrow(
                "SELECT * FROM tpdb_performers WHERE performer_id = $1", entity_id
            )
        else:
            raise KeyError(entity)

        if row is None:
            return None
        detail = dict(row)

        if entity == "sites":
            detail["scenes"] = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT scene_id AS id, title AS name, release_date AS date,
                           COALESCE(background_url, image_url, poster_url) AS image_url
                    FROM tpdb_scenes
                    WHERE site_id = $1
                    ORDER BY release_date DESC NULLS LAST
                    LIMIT 48
                    """,
                    entity_id,
                )
            ]
        elif entity == "networks":
            detail["sites"] = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT site_id AS id, name,
                           COALESCE(poster_url, logo_url) AS image_url
                    FROM tpdb_sites
                    WHERE network_id = $1
                    ORDER BY name
                    """,
                    entity_id,
                )
            ]
            detail["scenes"] = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT s.scene_id AS id, s.title AS name,
                           s.release_date AS date,
                           COALESCE(s.background_url, s.image_url, s.poster_url) AS image_url
                    FROM tpdb_scenes s
                    JOIN tpdb_sites st ON st.site_id = s.site_id
                    WHERE st.network_id = $1
                    ORDER BY s.release_date DESC NULLS LAST
                    LIMIT 48
                    """,
                    entity_id,
                )
            ]
        elif entity == "performers":
            detail["images"] = _performer_image_urls(detail)
            detail["scenes"] = [
                dict(r)
                for r in await conn.fetch(
                    """
                    SELECT s.scene_id AS id, s.title AS name,
                           s.release_date AS date,
                           COALESCE(s.background_url, s.image_url, s.poster_url) AS image_url
                    FROM tpdb_scenes s
                    JOIN tpdb_scene_performers sp ON sp.scene_id = s.scene_id
                    WHERE sp.performer_id = $1
                    ORDER BY s.release_date DESC NULLS LAST
                    LIMIT 48
                    """,
                    entity_id,
                )
            ]
        return detail


async def confirm_window_all_old(
    pool: asyncpg.Pool, floor_id: int, window: int, cutoff_at
) -> tuple[bool, int]:
    """Among torrents rows in [floor_id, floor_id+window-1], are all dated ones older than cutoff?
    Returns (all_old_and_at_least_one_dated_and_no_pending_holes, dated_count).

    A window is only "confirmed old" if it has no unresolved id_status entries
    (transient_error/parse_error) still awaiting a retry: those could yet turn
    into a torrent newer than the cutoff, so completing the backfill while they
    are outstanding could stop the backward walk prematurely."""
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
        pending_holes = await conn.fetchval(
            """
            SELECT count(*) FROM id_status
            WHERE torrent_id BETWEEN $1 AND $1 + $2 - 1
              AND status IN ('transient_error', 'parse_error')
            """,
            floor_id,
            window,
        )
    dated_count = row["dated_count"] or 0
    all_old = row["all_old"]
    return bool(dated_count > 0 and all_old and not pending_holes), dated_count
