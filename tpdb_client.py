"""Small TPDB client and response mapper for filename-based scene matching."""

import asyncio
import re
import time
import uuid
from datetime import date

import aiohttp

TPDB_BASE_URL = "https://api.theporndb.net"
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]+)?")
_SIZE_MULTIPLIERS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
}


class TPDBError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def size_text_to_bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = _SIZE_RE.match(value)
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    multiplier = _SIZE_MULTIPLIERS.get((match.group(2) or "b").lower())
    if multiplier is None:
        return None
    return int(amount * multiplier)


def largest_file(files) -> tuple[str, int | None] | None:
    if not files:
        return None
    ranked = []
    for item in files:
        if not isinstance(item, dict) or not item.get("filename"):
            continue
        size_bytes = size_text_to_bytes(item.get("size_text"))
        ranked.append((size_bytes if size_bytes is not None else -1, item["filename"]))
    if not ranked:
        return None
    size_bytes, filename = max(ranked, key=lambda item: item[0])
    return filename, size_bytes if size_bytes >= 0 else None


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _as_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _asset(raw: dict, key: str):
    value = raw.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("full") or value.get("large") or value.get("medium")
    return None


def _map_network(site_raw: dict) -> dict | None:
    raw = site_raw.get("network")
    if not raw and site_raw.get("network_id") and isinstance(site_raw.get("parent"), dict):
        parent = site_raw["parent"]
        if parent.get("id") == site_raw.get("network_id"):
            raw = parent
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
        return None
    return {
        "network_id": int(raw["id"]),
        "uuid": _as_uuid(raw.get("uuid")),
        "name": raw["name"],
        "short_name": raw.get("short_name"),
        "url": raw.get("url"),
        "description": raw.get("description"),
        "rating": raw.get("rating"),
        "logo_url": _asset(raw, "logo"),
        "favicon_url": _asset(raw, "favicon"),
        "poster_url": _asset(raw, "poster"),
        "metadata": raw,
    }


def _map_site(raw: dict, network: dict | None) -> dict | None:
    if not raw.get("id") or not raw.get("name"):
        return None
    return {
        "site_id": int(raw["id"]),
        "uuid": _as_uuid(raw.get("uuid")),
        "network_id": network["network_id"] if network else None,
        "parent_id": raw.get("parent_id"),
        "name": raw["name"],
        "short_name": raw.get("short_name"),
        "url": raw.get("url"),
        "description": raw.get("description"),
        "rating": raw.get("rating"),
        "logo_url": _asset(raw, "logo"),
        "favicon_url": _asset(raw, "favicon"),
        "poster_url": _asset(raw, "poster"),
        "metadata": raw,
    }


def _map_performer(site_profile: dict) -> dict | None:
    parent = site_profile.get("parent")
    raw = parent if isinstance(parent, dict) and parent.get("id") else site_profile
    performer_id = _as_uuid(raw.get("id"))
    if performer_id is None or not raw.get("name"):
        return None
    extras = raw.get("extras") or raw.get("extra") or {}
    return {
        "performer_id": performer_id,
        "tpdb_id": raw.get("_id"),
        "name": raw["name"],
        "slug": raw.get("slug"),
        "full_name": raw.get("full_name"),
        "disambiguation": raw.get("disambiguation"),
        "bio": raw.get("bio"),
        "rating": raw.get("rating"),
        "gender": extras.get("gender"),
        "birth_date": _as_date(extras.get("birthday")),
        "birthplace": extras.get("birthplace"),
        "nationality": extras.get("nationality"),
        "ethnicity": extras.get("ethnicity"),
        "hair_colour": extras.get("hair_colour") or extras.get("haircolor"),
        "eye_colour": extras.get("eye_colour"),
        "height": extras.get("height"),
        "weight": extras.get("weight"),
        "measurements": extras.get("measurements"),
        "cupsize": extras.get("cupsize"),
        "tattoos": extras.get("tattoos"),
        "piercings": extras.get("piercings"),
        "image_url": _asset(raw, "image"),
        "thumbnail_url": _asset(raw, "thumbnail"),
        "face_url": _asset(raw, "face"),
        "extras": extras,
        "posters": raw.get("posters"),
        "metadata": {"profile": raw, "site_profile": site_profile},
    }


def map_scene_record(raw: dict) -> dict:
    scene_id = _as_uuid(raw.get("id"))
    tpdb_id = raw.get("_id")
    if scene_id is None or tpdb_id is None or not raw.get("title"):
        raise ValueError("TPDB scene is missing id, _id, or title")

    site_raw = raw.get("site") if isinstance(raw.get("site"), dict) else {}
    network = _map_network(site_raw)
    site = _map_site(site_raw, network)
    performers = []
    seen = set()
    for profile in raw.get("performers") or []:
        if not isinstance(profile, dict):
            continue
        performer = _map_performer(profile)
        if performer and performer["performer_id"] not in seen:
            performers.append(performer)
            seen.add(performer["performer_id"])

    tags = [
        tag["name"]
        for tag in raw.get("tags") or []
        if isinstance(tag, dict) and tag.get("name")
    ]
    return {
        "network": network,
        "site": site,
        "performers": performers,
        "scene": {
            "scene_id": scene_id,
            "tpdb_id": int(tpdb_id),
            "title": raw["title"],
            "type": raw.get("type"),
            "slug": raw.get("slug"),
            "external_id": raw.get("external_id"),
            "description": raw.get("description"),
            "rating": raw.get("rating"),
            "release_date": _as_date(raw.get("date")),
            "url": raw.get("url"),
            "image_url": _asset(raw, "image"),
            "back_image_url": _asset(raw, "back_image"),
            "poster_url": _asset(raw, "poster") or _asset(raw, "posters"),
            "background_url": _asset(raw, "background"),
            "trailer_url": _asset(raw, "trailer"),
            "duration_seconds": raw.get("duration"),
            "format": raw.get("format"),
            "sku": raw.get("sku"),
            "tags": tags,
            "backgrounds": {
                "front": raw.get("background"),
                "back": raw.get("background_back"),
            },
            "hashes": raw.get("hashes"),
            "directors": raw.get("directors"),
            "links": raw.get("links"),
            "metadata": raw,
        },
    }


class TPDBClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        requests_per_second: float = 1.0,
    ) -> None:
        self.session = session
        self.api_key = api_key
        self.min_interval = 1.0 / max(requests_per_second, 0.01)
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def search_filename(self, filename: str) -> tuple[list[dict], int]:
        async with self._request_lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.session.get(
                    f"{TPDB_BASE_URL}/scenes",
                    params={"parse": filename, "per_page": 1},
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Accept": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    self._last_request_at = time.monotonic()
                    if response.status != 200:
                        body = await response.text()
                        raise TPDBError(
                            f"TPDB returned HTTP {response.status}: {body[:200]}",
                            response.status,
                        )
                    payload = await response.json()
            except asyncio.TimeoutError as exc:
                raise TPDBError("TPDB request timed out") from exc
            except aiohttp.ClientError as exc:
                raise TPDBError(f"TPDB request failed: {exc}") from exc

        data = payload.get("data")
        if not isinstance(data, list):
            raise TPDBError("TPDB response did not contain a data array", 200)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        try:
            total = int(meta.get("total", len(data)))
        except (TypeError, ValueError):
            total = len(data)
        return data, total
