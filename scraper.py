"""Fetch + parse a single /torrents/details/{id} page. See implementation.md field inventory / Data model."""

import email.utils
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiohttp import ClientTimeout
from bs4 import BeautifulSoup

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
DATE_FMT = "%d %b %Y %H:%M:%S"


class ParseError(Exception):
    pass


def parse_size(text: str) -> int | None:
    m = re.match(r"([\d.]+)\s*([KMGT]?B)", text.strip(), re.I)
    if not m:
        return None
    value, unit = m.groups()
    mult = SIZE_UNITS.get(unit.upper())
    if mult is None:
        return None
    return int(float(value) * mult)


MEDIA_INFO_TITLE_RE = re.compile(r"^media\s*info\s*:$", re.I)
MEDIA_INFO_SECTION_RE = re.compile(r"^([\w /]+?)\s*:$")
MEDIA_INFO_KV_RE = re.compile(r"^(.+?)\.{2,}\s*:\s*(.*)$")


def parse_media_info(text: str) -> dict | None:
    """Caller confirms a MediaInfo block exists (font[style*=monospace] found) before calling this ---
    the title line's exact wording varies across releases ("Media Info :" / "Mediainfo :"), so don't gate on it."""
    lines = [ln.strip() for ln in text.split("\n")]
    result: dict = {}
    section = result
    for ln in lines:
        if not ln or MEDIA_INFO_TITLE_RE.match(ln):
            continue
        kv = MEDIA_INFO_KV_RE.match(ln)
        if kv:
            section[kv.group(1).strip()] = kv.group(2).strip()
            continue
        sec = MEDIA_INFO_SECTION_RE.match(ln)
        if sec:
            section = result.setdefault(sec.group(1).strip(), {})
    return result or None


def decode_cfemail(hexstr: str) -> str:
    """Reverse Cloudflare's email-obfuscation XOR encoding (triggered by any '@' in server-rendered text,
    not just real emails -- e.g. a MediaInfo 'Format profile' value like 'High@L5.1' gets mangled too)."""
    key = int(hexstr[:2], 16)
    return "".join(
        chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2)
    )


def deobfuscate_cf_emails(soup: BeautifulSoup) -> None:
    for a in soup.find_all("a", class_="__cf_email__"):
        cfemail = a.get("data-cfemail")
        a.replace_with(decode_cfemail(cfemail) if cfemail else "")


def parse_datetime(text: str, tz_name: str) -> datetime | None:
    text = text.strip()
    if not text or text.lower() == "pending":
        return None
    try:
        naive = datetime.strptime(text, DATE_FMT)
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(tz_name))


def parse_details_html(html: str, torrent_id: int, tz_name: str = "UTC") -> dict:
    soup = BeautifulSoup(html, "lxml")
    deobfuscate_cf_emails(soup)

    detailsdiv = soup.select_one("div.detailsdiv")
    if not detailsdiv:
        raise ParseError("missing div.detailsdiv")

    descr = detailsdiv.select_one(".detailsdescr")
    if not descr:
        raise ParseError("missing .detailsdescr")

    h1 = detailsdiv.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        raise ParseError("missing title")

    category = None
    size_bytes = None
    added_at_raw = None
    seeders = leechers = 0
    last_scraped_raw = None
    uploader = None
    downloads = None
    tags = []
    magnet = None
    torrent_download_hash = None

    for li in descr.select("ul > li"):
        classes = li.get("class") or []
        if "downloadboxlist" in classes:
            a_dl = li.find("a", href=re.compile(r"^/torrents/download/"))
            if a_dl:
                m = re.search(r"/torrents/download/([a-f0-9]{40})", a_dl["href"], re.I)
                if m:
                    torrent_download_hash = m.group(1).lower()
            a_mag = li.find("a", href=re.compile(r"^magnet:"))
            if a_mag:
                magnet = a_mag["href"]
            continue

        spans = li.find_all("span", recursive=False)
        if len(spans) < 3:
            continue
        label = spans[0].get_text(strip=True)
        value_span = spans[2]

        if label == "Category":
            a = value_span.find("a")
            category = a.get_text(strip=True) if a else value_span.get_text(strip=True)
        elif label == "Size":
            size_bytes = parse_size(value_span.get_text(strip=True))
        elif label == "Added Date":
            added_at_raw = value_span.get_text(strip=True)
        elif label == "Peers":
            see = value_span.find("font", class_="see")
            lee = value_span.find("font", class_="lee")
            seeders = int(see.get_text(strip=True)) if see and see.get_text(strip=True).isdigit() else 0
            leechers = int(lee.get_text(strip=True)) if lee and lee.get_text(strip=True).isdigit() else 0
        elif label == "Last Scraped":
            last_scraped_raw = value_span.get_text(strip=True)
        elif label == "Uploader":
            uploader = value_span.get_text(strip=True)
        elif label == "Downloads":
            dtext = value_span.get_text(strip=True)
            downloads = int(dtext) if dtext.isdigit() else None
        elif label == "Collection":
            for a in value_span.find_all("a"):
                tags.append(a.get_text(strip=True))

    if not magnet:
        raise ParseError("missing magnet link")

    info_hash_match = re.search(r"btih:([a-f0-9]{40})", magnet, re.I)
    info_hash = info_hash_match.group(1).lower() if info_hash_match else torrent_download_hash

    poster = detailsdiv.select_one("img.detailsposter")
    image_url = poster.get("src") if poster else None

    likes = dislikes = 0
    rating = detailsdiv.select_one(".rating-system")
    if rating:
        for div in rating.select(".rating"):
            a = div.find("a")
            span = div.find("span")
            count_text = span.get_text(strip=True) if span else ""
            count = int(count_text) if count_text.isdigit() else 0
            classes = a.get("class") or [] if a else []
            if "like" in classes:
                likes = count
            elif "dislike" in classes:
                dislikes = count

    files = []
    filestable = detailsdiv.select_one(".filestable")
    if filestable:
        rows = filestable.select("ul > li")
        for li in rows[1:]:
            spans = li.find_all("span", recursive=False)
            if len(spans) >= 2:
                files.append({
                    "filename": spans[0].get_text(strip=True),
                    "size_text": spans[1].get_text(strip=True),
                })

    description_text = None
    media_info = None
    images = []
    descr_div = detailsdiv.select_one(".description")
    if descr_div:
        for img in descr_div.find_all("img"):
            src = img.get("src")
            if src:
                images.append(src.replace("/1s/", "/1/"))

        font = descr_div.find("font", style=re.compile(r"monospace"))
        if font:
            for br in font.find_all("br"):
                br.replace_with("\n")
            media_info = parse_media_info(font.get_text())
            font.decompose()

        for a in descr_div.find_all("a"):
            if a.find("img"):
                a.decompose()

        for node in descr_div.find_all(string=re.compile(r"^\s*Screenshots\s*:\s*$")):
            node.extract()

        for br in descr_div.find_all("br"):
            br.replace_with("\n")

        raw_text = descr_div.get_text()
        description_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()

    added_at = parse_datetime(added_at_raw, tz_name) if added_at_raw else None
    if added_at is None:
        raise ParseError(f"missing/unparseable Added Date: {added_at_raw!r}")
    last_scraped = parse_datetime(last_scraped_raw, tz_name) if last_scraped_raw else None

    return {
        "torrent_id": torrent_id,
        "info_hash": info_hash,
        "title": title,
        "category": category,
        "size_bytes": size_bytes,
        "added_at": added_at,
        "seeders": seeders,
        "leechers": leechers,
        "last_scraped": last_scraped,
        "uploader": uploader,
        "downloads": downloads,
        "tags": tags,
        "magnet": magnet,
        "image_url": image_url,
        "images": images,
        "description_text": description_text,
        "media_info": media_info,
        "files": files,
        "likes": likes,
        "dislikes": dislikes,
    }


def looks_like_details_page(html: str) -> bool:
    return 'class="detailsdiv"' in html or "class=\"detailsdescr\"" in html


def looks_like_soft_404(html: str) -> bool:
    """The site serves 'not found' as HTTP 200 with its normal chrome plus an errordiv.

    Non-existent IDs never return a real 404 status -- verified live: /details/460000
    is `200` carrying `<div class="errordiv"><h1>Error :</h1> ... 404 : Not Found`.
    This is a normal data outcome, not a block, so it must not cool the host.
    """
    return 'class="errordiv"' in html


# Cloudflare's beacon script (/cdn-cgi/challenge-platform/...) is present on EVERY
# page including valid ones, so the word "challenge" alone is useless as a marker.
# These are interstitial-only.
CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "_cf_chl_opt",
    'id="challenge-form"',
    "cf-error-details",
    "Attention Required! | Cloudflare",
    "Just a moment...",
)


def looks_like_challenge(html: str) -> bool:
    """Positively identify a block/interstitial. Anything merely unrecognized is NOT
    treated as a block -- guessing that way is what stalls the crawler."""
    return any(marker in html for marker in CHALLENGE_MARKERS)


def parse_retry_after(value: str | None) -> float | None:
    """Both header forms -> seconds. Returns None for absent/malformed/non-finite values.

    The HTTP-date form can yield a negative result (past date, or clock skew): that is
    returned as-is and normalized by the caller, which knows what floor to apply.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_dt = email.utils.parsedate_to_datetime(value)
            seconds = (retry_dt - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return None
    return seconds if math.isfinite(seconds) else None


async def fetch_page(session, url: str, timeout: float = 15.0):
    """Returns (status, html_or_none, redirect_location_or_none, retry_after_seconds_or_none).

    Redirects are never followed (see implementation.md); a 3xx returns its Location instead of a body.
    """
    async with session.get(url, allow_redirects=False, timeout=ClientTimeout(total=timeout)) as resp:
        retry_after_seconds = parse_retry_after(resp.headers.get("Retry-After"))
        if resp.status in (301, 302, 303, 307, 308):
            return resp.status, None, resp.headers.get("Location"), retry_after_seconds
        text = await resp.text()
        return resp.status, text, None, retry_after_seconds


def details_path(torrent_id: int) -> str:
    return f"/torrents/details/{torrent_id}"


async def fetch_details(session, base_url: str, torrent_id: int, timeout: float = 15.0):
    """Kept for probe_limit.py, which deliberately bypasses the host pool."""
    return await fetch_page(session, f"{base_url}{details_path(torrent_id)}", timeout)
