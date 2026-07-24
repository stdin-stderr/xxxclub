"""Read-only web UI over the torrents table. Serves the archive/detail views; no writes, no scraping.

Templates live in web/templates/, adapted from designs/archive.html + designs/detail.html (see CLAUDE.md).
"""

import logging
import math
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode

import jinja2
from aiohttp import web

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("webapp")

TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
PAGE_SIZE = 24
SIZE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"]


def format_size(n) -> str:
    if n is None:
        return "—"
    size = float(n)
    unit_i = 0
    while size >= 1024 and unit_i < len(SIZE_UNITS) - 1:
        size /= 1024
        unit_i += 1
    if unit_i == 0:
        return f"{int(size)} {SIZE_UNITS[unit_i]}"
    return f"{size:.2f} {SIZE_UNITS[unit_i]}"


def format_duration(seconds) -> str:
    if seconds is None:
        return "—"
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_date(dt, with_time: bool = False) -> str:
    if dt is None:
        return "Pending"
    if with_time:
        return dt.strftime("%b %d, %Y, %H:%M UTC")
    return dt.strftime("%b %d, %Y")


def format_datetime_full(dt) -> str:
    if dt is None:
        return "Pending"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=jinja2.select_autoescape(["html"]),
)
env.filters["size"] = format_size
env.filters["duration"] = format_duration
env.filters["date"] = format_date
env.filters["datetime_full"] = format_datetime_full


def render(name: str, **ctx) -> web.Response:
    template = env.get_template(name)
    return web.Response(text=template.render(**ctx), content_type="text/html")


SORT_LABELS = {
    "added_desc": "Added Date, descending",
    "added_asc": "Added Date, ascending",
    "seeders_desc": "Seeders, descending",
    "size_desc": "Size, descending",
    "downloads_desc": "Downloads, descending",
}

CATALOG_LABELS = {
    "scenes": {
        "singular": "Scene",
        "plural": "Scenes",
        "related": "performers",
    },
    "sites": {
        "singular": "Site",
        "plural": "Sites",
        "related": "scenes",
    },
    "networks": {
        "singular": "Network",
        "plural": "Networks",
        "related": "sites",
    },
    "performers": {
        "singular": "Performer",
        "plural": "Performers",
        "related": "scenes",
    },
}


async def archive_handler(request: web.Request) -> web.Response:
    pool = request.app["pool"]
    q = request.query.get("q", "").strip() or None
    category = request.query.get("category") or None
    tag = request.query.get("tag") or None
    sort = request.query.get("sort") or db.DEFAULT_SORT
    if sort not in db.SORT_OPTIONS:
        sort = db.DEFAULT_SORT
    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    offset = (page - 1) * PAGE_SIZE

    total = await db.count_torrents(pool, q=q, category=category, tag=tag)
    total_all = await db.count_torrents(pool)
    torrents = await db.list_torrents(
        pool, q=q, category=category, tag=tag, sort=sort, limit=PAGE_SIZE, offset=offset
    )
    categories = await db.distinct_categories(pool)
    tags = await db.top_tags(pool)

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    start = offset + 1 if total else 0
    end = min(offset + PAGE_SIZE, total)

    def page_url(**overrides) -> str:
        params = {"q": q, "category": category, "tag": tag, "sort": sort, "page": page}
        params.update(overrides)
        params = {k: v for k, v in params.items() if v}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"/torrents/?{query}" if query else "/torrents/"

    return render(
        "archive.html",
        torrents=torrents,
        q=q or "",
        category=category,
        tag=tag,
        sort=sort,
        sort_options=SORT_LABELS,
        categories=categories,
        tags=tags,
        total_fmt=f"{total:,}",
        total_all_fmt=f"{total_all:,}",
        start=start,
        end=end,
        page=page,
        total_pages=total_pages,
        page_url=page_url,
    )


async def tags_handler(request: web.Request) -> web.Response:
    pool = request.app["pool"]
    tags = await db.top_tags(pool, limit=None)
    total_all = await db.count_torrents(pool)
    return render("tags.html", tags=tags, total_all_fmt=f"{total_all:,}")


async def detail_handler(request: web.Request) -> web.Response:
    pool = request.app["pool"]
    try:
        torrent_id = int(request.match_info["torrent_id"])
    except ValueError:
        raise web.HTTPNotFound()
    t = await db.get_torrent(pool, torrent_id)
    if t is None:
        raise web.HTTPNotFound(text="Torrent not found")
    total_all = await db.count_torrents(pool)
    return render("detail.html", t=t, total_all_fmt=f"{total_all:,}")


async def catalog_archive_handler(request: web.Request) -> web.Response:
    entity = request.match_info.get("entity", "scenes")
    if entity not in CATALOG_LABELS:
        raise web.HTTPNotFound()
    pool = request.app["pool"]
    q = request.query.get("q", "").strip() or None
    tag = (
        request.query.get("tag", "").strip() or None
        if entity == "scenes"
        else None
    )
    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    offset = (page - 1) * PAGE_SIZE

    total = await db.count_catalog_entities(pool, entity, q, tag)
    records = await db.list_catalog_entities(
        pool, entity, q=q, tag=tag, limit=PAGE_SIZE, offset=offset
    )
    tags = await db.top_scene_tags(pool) if entity == "scenes" else []
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    total_all = await db.count_torrents(pool)
    archive_path = "/" if entity == "scenes" else f"/{entity}"

    def page_url(**overrides) -> str:
        params = {"q": q, "tag": tag, "page": page}
        params.update(overrides)
        query = urlencode({k: v for k, v in params.items() if v})
        return f"{archive_path}?{query}" if query else archive_path

    return render(
        "catalog_archive.html",
        entity=entity,
        labels=CATALOG_LABELS[entity],
        records=records,
        q=q or "",
        tag=tag,
        tags=tags,
        total=total,
        total_fmt=f"{total:,}",
        total_all_fmt=f"{total_all:,}",
        start=offset + 1 if total else 0,
        end=min(offset + PAGE_SIZE, total),
        page=page,
        total_pages=total_pages,
        page_url=page_url,
    )


async def catalog_detail_handler(request: web.Request) -> web.Response:
    entity = request.match_info["entity"]
    if entity not in CATALOG_LABELS:
        raise web.HTTPNotFound()
    raw_id = request.match_info["entity_id"]
    try:
        entity_id = (
            uuid.UUID(raw_id) if entity in {"scenes", "performers"} else int(raw_id)
        )
    except (ValueError, TypeError):
        raise web.HTTPNotFound()

    pool = request.app["pool"]
    record = await db.get_catalog_detail(pool, entity, entity_id)
    if record is None:
        raise web.HTTPNotFound(text=f"{CATALOG_LABELS[entity]['singular']} not found")
    total_all = await db.count_torrents(pool)
    return render(
        "catalog_detail.html",
        entity=entity,
        labels=CATALOG_LABELS[entity],
        record=record,
        total_all_fmt=f"{total_all:,}",
    )


async def scenes_redirect_handler(request: web.Request) -> web.Response:
    raise web.HTTPMovedPermanently(location="/")


def dsn_from_env() -> str:
    return (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'xxxclub')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'xxxclub')}"
        f"@{os.environ.get('POSTGRES_HOST', 'db')}:{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('POSTGRES_DB', 'xxxclub')}"
    )


async def make_app() -> web.Application:
    pool = await db.create_pool(dsn_from_env())
    app = web.Application()
    app["pool"] = pool
    app.add_routes([
        web.get("/", catalog_archive_handler),
        web.get("/torrents", archive_handler),
        web.get("/torrents/", archive_handler),
        web.get("/scenes", scenes_redirect_handler),
        web.get("/scenes/", scenes_redirect_handler),
        web.get("/tags", tags_handler),
        web.get("/torrent/{torrent_id}", detail_handler),
        web.get("/{entity:scenes|sites|networks|performers}", catalog_archive_handler),
        web.get(
            "/{entity:scenes|sites|networks|performers}/{entity_id}",
            catalog_detail_handler,
        ),
    ])

    async def close_pool(app: web.Application) -> None:
        await app["pool"].close()

    app.on_cleanup.append(close_pool)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", "8080"))
    log.info("starting web UI on port %d", port)
    web.run_app(make_app(), host="0.0.0.0", port=port)
