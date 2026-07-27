"""Env config loading and validation, kept free of DB/HTTP imports so it is testable alone.

Validation here is defensive, not adaptive: it rejects values that cannot work (a zero
rate divides by zero, `inf` yields a zero interval, a duplicate host silently doubles
that domain's rate) rather than trying to tune anything at runtime. See implementation.md "Config".
"""

import logging
import math
import os
from urllib.parse import quote_plus, urlsplit

log = logging.getLogger("config")

SECRET_KEYS = {"POSTGRES_PASSWORD"}

# Adaptive limiting is gone (see implementation.md "Rate limiting and host pool"). Silently ignoring these would turn a
# deliberately-tuned MAX_REQUESTS_PER_SECOND=0.5 into the new default of 1 without a
# word in the log -- a silent doubling of request rate against the site.
REMOVED_KEYS = {
    "MAX_CONCURRENCY": "removed; concurrency is now one in-flight request per domain in BASE_URLS",
    "MAX_CONCURRENCY_CEILING": "removed; there is no adaptive concurrency ramp",
    "MAX_REQUESTS_PER_SECOND": "renamed to REQUESTS_PER_SECOND_PER_DOMAIN",
    "MAX_REQUESTS_PER_SECOND_CEILING": "removed; there is no adaptive rate ramp",
}

MIN_RATE = 0.01  # one request per 100s; also bounds the derived interval


class ConfigError(Exception):
    pass


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}={raw!r} is not an integer")


def env_positive_float(key: str, default: float, minimum: float = 0.0) -> float:
    """float() accepts 'inf' and 'nan'; neither is a usable rate, cooldown or timeout."""
    raw = os.environ.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}={raw!r} is not a number")
    if not math.isfinite(value):
        raise ConfigError(f"{key}={raw!r} must be finite")
    if value <= 0:
        raise ConfigError(f"{key}={raw!r} must be greater than 0")
    if value < minimum:
        raise ConfigError(f"{key}={raw!r} is below the minimum supported value of {minimum}")
    return value


def check_removed_keys() -> None:
    present = sorted(k for k in REMOVED_KEYS if os.environ.get(k))
    if present:
        details = "\n".join(f"  {k} -- {REMOVED_KEYS[k]}" for k in present)
        raise ConfigError(
            "these settings no longer exist and must be removed from .env and the "
            f"compose files:\n{details}"
        )


def normalize_base_url(raw: str) -> str:
    """Reject anything that is not a bare http(s) origin, and normalize for comparison."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"BASE_URLS entry {raw!r} must use http or https")
    if not parts.hostname:
        raise ConfigError(f"BASE_URLS entry {raw!r} has no host")
    if parts.username or parts.password:
        raise ConfigError(f"BASE_URLS entry {raw!r} must not contain credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError(f"BASE_URLS entry {raw!r} must be a bare origin, with no path or query")
    netloc = parts.hostname.lower()
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return f"{parts.scheme.lower()}://{netloc}"


def parse_base_urls() -> list[str]:
    raw = os.environ.get("BASE_URLS") or os.environ.get("BASE_URL") or "https://xxxclub.to"
    urls: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            raise ConfigError(f"BASE_URLS={raw!r} contains an empty entry")
        url = normalize_base_url(entry)
        if url in urls:
            # Two queue slots for one domain would double that domain's concurrency and
            # rate while the startup log still reported the intended per-domain figure.
            raise ConfigError(f"BASE_URLS={raw!r} lists {url} more than once")
        urls.append(url)
    if not urls:
        raise ConfigError("BASE_URLS is empty")
    return urls


def load_config() -> dict:
    check_removed_keys()
    return {
        "POSTGRES_USER": os.environ.get("POSTGRES_USER", "xxxclub"),
        "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", "xxxclub"),
        "POSTGRES_DB": os.environ.get("POSTGRES_DB", "xxxclub"),
        "POSTGRES_HOST": os.environ.get("POSTGRES_HOST", "db"),
        "POSTGRES_PORT": env_int("POSTGRES_PORT", 5432),
        "BASE_URLS": parse_base_urls(),
        "SITE_TZ": os.environ.get("SITE_TZ", "UTC"),
        "BACKFILL_DAYS": env_int("BACKFILL_DAYS", 30),
        "REQUESTS_PER_SECOND_PER_DOMAIN": env_positive_float(
            "REQUESTS_PER_SECOND_PER_DOMAIN", 1.0, minimum=MIN_RATE
        ),
        "BLOCK_COOLDOWN_SECONDS": env_positive_float("BLOCK_COOLDOWN_SECONDS", 60.0),
        "REQUEST_TIMEOUT_SECONDS": env_positive_float("REQUEST_TIMEOUT_SECONDS", 15.0),
        "FRONTIER_LOOKAHEAD": env_int("FRONTIER_LOOKAHEAD", 500),
        "FRONTIER_DRY_STREAK": env_int("FRONTIER_DRY_STREAK", 200),
        "BACKFILL_CHUNK_SIZE": env_int("BACKFILL_CHUNK_SIZE", 500),
        "BACKFILL_CONFIRM_WINDOW": env_int("BACKFILL_CONFIRM_WINDOW", 100),
        "BACKFILL_SAFETY_MARGIN_IDS": env_int("BACKFILL_SAFETY_MARGIN_IDS", 200),
        "RETRY_BATCH_SIZE": env_int("RETRY_BATCH_SIZE", 200),
        "REFRESH_BATCH_SIZE": env_int("REFRESH_BATCH_SIZE", 200),
        "CYCLE_INTERVAL": env_int("CYCLE_INTERVAL", 60),
    }


def log_config(cfg: dict) -> None:
    masked = {k: ("***" if k in SECRET_KEYS else v) for k, v in cfg.items()}
    for k, v in masked.items():
        log.info("config %s = %s", k, v)

    rate = cfg["REQUESTS_PER_SECOND_PER_DOMAIN"]
    domains = len(cfg["BASE_URLS"])
    log.info(
        "pacing: 1 request every %.2fs per domain across %d domain(s) = %.2f req/s total",
        1.0 / rate, domains, rate * domains,
    )


def dsn(cfg: dict) -> str:
    user = quote_plus(cfg["POSTGRES_USER"])
    password = quote_plus(cfg["POSTGRES_PASSWORD"])
    return (
        f"postgresql://{user}:{password}"
        f"@{cfg['POSTGRES_HOST']}:{cfg['POSTGRES_PORT']}/{cfg['POSTGRES_DB']}"
    )
