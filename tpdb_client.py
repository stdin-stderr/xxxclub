"""Small TPDB client and response mapper for filename-based scene matching."""

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date

import aiohttp

TPDB_BASE_URL = "https://api.theporndb.net"
_SIZE_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z]+)?\s*$")
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
_VIDEO_EXTENSION_RE = re.compile(r"\.(?:mp4|mkv|avi|wmv|mov|m4v|ts)$", re.I)
_SCENE_KEY_NOISE_RE = re.compile(
    r"(?:[._ -](?:480p|720p|1080p|2160p|4k|uhd|fhd|fullhd|"
    r"x26[45]|h26[45]|hevc|web[-._ ]?dl|webrip))+$",
    re.I,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MATCH_NOISE = {
    "a",
    "aka",
    "and",
    "avi",
    "for",
    "from",
    "fhd",
    "fullhd",
    "h264",
    "h265",
    "hevc",
    "her",
    "his",
    "in",
    "m4v",
    "mkv",
    "mov",
    "mp4",
    "my",
    "nl",
    "of",
    "on",
    "p2p",
    "rm",
    "the",
    "to",
    "ts",
    "uhd",
    "web",
    "webdl",
    "webrip",
    "with",
    "wmv",
    "wrb",
    "xc",
    "x264",
    "x265",
    "xxx",
    "480p",
    "720p",
    "1080p",
    "2160p",
    "4k",
}


@dataclass(frozen=True)
class MatchSource:
    site_label: str
    release_date: date | None
    content: str
    tokens: frozenset[str]
    scene_key: str
    queries: tuple[str, ...]


@dataclass(frozen=True)
class CandidateDecision:
    candidate: dict | None
    score: float
    accepted: bool
    reason: str
    audit: list[dict]


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
        amount = float(match.group(1).replace(",", ""))
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
        filename = item["filename"]
        ranked.append(
            (
                bool(_VIDEO_EXTENSION_RE.search(filename)),
                size_bytes if size_bytes is not None else -1,
                filename,
            )
        )
    if not ranked:
        return None
    _, size_bytes, filename = max(ranked, key=lambda item: (item[0], item[1]))
    return filename, size_bytes if size_bytes >= 0 else None


def scene_key(filename: str) -> str:
    basename = re.split(r"[/\\]", filename or "")[-1].lower()
    basename = re.sub(r"\.(?:mp4|mkv|avi|wmv|mov|m4v|ts)$", "", basename)
    basename = _SCENE_KEY_NOISE_RE.sub("", basename)
    return ".".join(_TOKEN_RE.findall(basename))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _TOKEN_RE.findall((value or "").lower())
        if len(token) > 1 and token not in _MATCH_NOISE
    )


def _date_and_content(value: str) -> tuple[date | None, list[str]]:
    parts = _TOKEN_RE.findall((value or "").lower())
    for index in range(len(parts) - 2):
        triple = parts[index : index + 3]
        if all(re.fullmatch(r"\d{2}", part) for part in triple):
            year, month, day = map(int, triple)
            try:
                return date(2000 + year, month, day), parts[index + 3 :]
            except ValueError:
                continue
        if (
            len(triple[0]) == 2
            and len(triple[1]) == 2
            and len(triple[2]) == 4
            and all(part.isdigit() for part in triple)
        ):
            day, month, year = map(int, triple)
            try:
                return date(year, month, day), parts[index + 3 :]
            except ValueError:
                continue
    return None, parts


def build_match_source(filename: str, torrent_title: str) -> MatchSource:
    site_label = (torrent_title or "").split(maxsplit=1)[0]
    filename_date, content_parts = _date_and_content(filename)
    title_date, title_parts = _date_and_content(torrent_title)
    # Some uploaders prepend their own upload date to the filename. The title's
    # date is the scene date and is therefore authoritative when both exist.
    release_date = title_date or filename_date

    content_tokens = [
        part for part in content_parts if len(part) > 1 and part not in _MATCH_NOISE
    ]
    title_tokens = [
        part
        for part in title_parts
        if len(part) > 1
        and part not in _MATCH_NOISE
        and _normalise_name(part) != _normalise_name(site_label)
    ]
    content = " ".join(content_tokens)
    scoring_text = content
    if title_date and filename_date and title_date != filename_date:
        scoring_text = " ".join(title_tokens)
    elif not content_tokens:
        scoring_text = " ".join(title_tokens)
    combined_tokens = _tokens(scoring_text)

    query_candidates = [
        content,
        " ".join(content_tokens[:3]),
        " ".join(content_tokens[-8:]),
        " ".join(title_tokens),
        " ".join(title_tokens[:3]),
        " ".join(title_tokens[-8:]),
    ]
    queries = []
    seen = set()
    for query in query_candidates:
        query = " ".join(query.split())
        normalised = query.lower()
        if len(query) >= 4 and normalised not in seen:
            queries.append(query)
            seen.add(normalised)

    return MatchSource(
        site_label=site_label,
        release_date=release_date,
        content=content,
        tokens=combined_tokens,
        scene_key=scene_key(filename),
        queries=tuple(queries),
    )


def _normalise_name(value: str | None) -> str:
    return "".join(_TOKEN_RE.findall((value or "").lower()))


def select_site(sites: list[dict], label: str) -> dict | None:
    target = _normalise_name(label)
    exact = [
        site
        for site in sites
        if _normalise_name(site.get("name")) == target
        or _normalise_name(site.get("short_name")) == target
    ]
    if exact:
        return exact[0]
    return sites[0] if len(sites) == 1 else None


def _candidate_score(
    candidate: dict,
    source: MatchSource,
    expected_site: dict | None,
) -> tuple[float, bool, str, dict]:
    raw_site = candidate.get("site") if isinstance(candidate.get("site"), dict) else {}
    site_match = False
    if expected_site and expected_site.get("id"):
        site_match = str(raw_site.get("id")) == str(expected_site["id"])
    if not site_match:
        source_site = _normalise_name(source.site_label)
        site_match = source_site in {
            _normalise_name(raw_site.get("name")),
            _normalise_name(raw_site.get("short_name")),
        }

    candidate_text = " ".join(
        [
            candidate.get("title") or "",
            *[
                performer.get("name") or ""
                for performer in candidate.get("performers") or []
                if isinstance(performer, dict)
            ],
        ]
    )
    candidate_tokens = _tokens(candidate_text)
    overlap = len(source.tokens & candidate_tokens) / max(
        1, min(len(source.tokens), len(candidate_tokens))
    )

    candidate_date = _as_date(candidate.get("date"))
    date_delta = None
    if source.release_date and candidate_date:
        date_delta = abs((candidate_date - source.release_date).days)

    score = overlap * 0.35
    if site_match:
        score += 0.35
    if date_delta == 0:
        score += 0.30
    elif date_delta is not None and date_delta <= 3:
        score += 0.20
    elif date_delta is not None and date_delta <= 7:
        score += 0.10

    accepted = bool(
        (date_delta == 0 and overlap >= 0.5 and (site_match or overlap >= 0.8))
        or (
            date_delta is not None
            and date_delta <= 3
            and overlap >= 0.8
            and (site_match or overlap >= 0.9)
        )
        or (
            site_match
            and date_delta is not None
            and date_delta <= 7
            and overlap >= 0.8
        )
    )
    reason = (
        f"site={'yes' if site_match else 'no'} "
        f"date_delta={date_delta if date_delta is not None else 'unknown'} "
        f"token_overlap={overlap:.2f}"
    )
    audit = {
        "id": candidate.get("id"),
        "title": candidate.get("title"),
        "date": str(candidate_date) if candidate_date else None,
        "site": raw_site.get("name"),
        "score": round(score, 4),
        "accepted": accepted,
        "reason": reason,
    }
    return score, accepted, reason, audit


def choose_candidate(
    candidates: list[dict],
    source: MatchSource,
    expected_site: dict | None = None,
) -> CandidateDecision:
    ranked = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        raw_site = candidate.get("site") if isinstance(candidate.get("site"), dict) else {}
        semantic_key = (
            _normalise_name(candidate.get("title")),
            str(_as_date(candidate.get("date")) or ""),
            _normalise_name(raw_site.get("name")),
        )
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        score, accepted, reason, audit = _candidate_score(
            candidate, source, expected_site
        )
        ranked.append((score, accepted, reason, candidate, audit))

    ranked.sort(key=lambda item: item[0], reverse=True)
    audit = [item[4] for item in ranked[:10]]
    if not ranked:
        return CandidateDecision(None, 0.0, False, "no candidates", audit)

    best = ranked[0]
    margin = best[0] - ranked[1][0] if len(ranked) > 1 else 1.0
    accepted = best[1] and margin >= 0.08
    reason = best[2]
    if best[1] and not accepted:
        reason = f"ambiguous top candidates (margin={margin:.3f}); {reason}"
    return CandidateDecision(best[3], best[0], accepted, reason, audit)


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

    async def _get_list(self, path: str, params: dict) -> tuple[list[dict], int]:
        async with self._request_lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.session.get(
                    f"{TPDB_BASE_URL}{path}",
                    params=params,
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

    async def search_filename(
        self, filename: str, per_page: int = 10
    ) -> tuple[list[dict], int]:
        return await self._get_list(
            "/scenes",
            {"parse": filename, "per_page": per_page},
        )

    async def search_scenes(
        self, query: str, per_page: int = 10
    ) -> tuple[list[dict], int]:
        return await self._get_list(
            "/scenes",
            {"q": query, "per_page": per_page},
        )

    async def search_sites(
        self, query: str, per_page: int = 10
    ) -> tuple[list[dict], int]:
        return await self._get_list(
            "/sites",
            {"q": query, "per_page": per_page},
        )

    async def site_scenes(
        self, site_id: int, per_page: int = 25
    ) -> tuple[list[dict], int]:
        return await self._get_list(
            f"/sites/{site_id}/scenes",
            {"per_page": per_page},
        )
