# xxxclub V2 — implementation

Design rationale and data model for the scraper, crawler, host pool, and TPDB
matcher. See [CLAUDE.md](CLAUDE.md) for the quick-reference layout, running
instructions, and the short invariant list. This file is the detail behind those
invariants.

## Core design

`/torrents/details/{id}` uses a sequential numeric ID — no pagination, no
hash-based decoys. Backfill and frontier-tracking work by walking IDs, not pages.
ID↔date is only approximately monotonic (a low ID can carry a later date than a
higher one), so nothing in the code treats it as a proof of range completeness or
uses binary search over the date boundary — gaps make that invalid.

## What's on one details page (from the saved `450462.html` fixture)

- `h1` title
- Category (text + numeric id from `/torrents/browse/{n}/` href) — both stored
- Size (text, e.g. "532.59 MB") — parsed to `size_bytes`
- Added Date (e.g. "23 Jul 2026 13:46:16") — no timezone in the markup; localized via `SITE_TZ` (default `UTC`), a documented assumption
- Peers: `<font class="see">` seeders, `<font class="lee">` leechers
- Last Scraped — dated timestamp, or "Pending", or absent — stored as a single nullable timestamptz, `NULL` for anything not a parseable date
- Uploader, Downloads (count)
- Collection/tags — one or more `<li>` rows, link text only kept (href/slug dropped)
- Torrent download link: `/torrents/download/{40-char-hash}` (real SHA1 payload hash)
- Magnet URI — taken verbatim from the page, not rebuilt (rebuilding risks percent-encoding the `btih:` colons, which some clients reject)
- `og:image` / `.detailsposter` src — main poster image
- Description block (`.description`) — screenshot `<a><img>` markup stripped (URLs extracted into `images`, transformed `/1s/` → `/1/` for full-res), MediaInfo `<font>` block stripped and parsed into `media_info`, remainder (release-name line etc.) kept as plain text (`<br>` → `\n`) in `description_text`
- Files list (`.filestable`) — `<li>` pairs of filename + size, stored as `files` jsonb
- Likes / Dislikes (`.rating-system`)
- "Similar Torrents" (`.similardiv`) is not stored. Parsing is scoped to
  `.detailsdescr`/`.rating-system`/`.filestable`/`.description` specifically so
  this sidebar can never bleed into main-page fields.

Meta tags are partial fallbacks only — they don't restate uploader, peers,
ratings, files, magnet, or description.

## Crawl state (`crawl_state`, single row)

Crawl progress is never inferred from `torrents` contents — a recent row can't
distinguish a completed backfill from a crashed one-row bootstrap, and one old row
isn't evidence a cutoff was fully scanned.

| Field | Purpose |
|---|---|
| `highest_success_id` | highest `torrent_id` ever successfully parsed |
| `contiguous_floor_id` | lowest `torrent_id` such that **every** ID from here up to `highest_success_id` is resolved — either a `torrents` row or an `id_status` ledger row. The real "have we covered this range" watermark. |
| `backfill_cutoff_at` | the date boundary backfill is walking down toward (`now() - BACKFILL_DAYS`, fixed at bootstrap so raising `BACKFILL_DAYS` mid-run doesn't move the goalposts under a live run) |
| `backfill_completed_at` | null while in progress; set once the confirm-window rule below is satisfied |
| `frontier_scan_high` | highest ID a forward probe has reached, so cycles resume the lookahead window instead of restarting from `highest_success_id + 1` every time |
| `backfill_confirm_anchor_id` | floor ID at which the confirm window first went all-old; reset to null if a newer row appears in the window before the safety margin is met |

`contiguous_floor_id` only moves down when every single ID in the gap has a
resolution (success or ledger entry) — no skipping. This makes "increase
`BACKFILL_DAYS` later" naturally extend the walk downward from wherever it
stopped, and makes an interrupted run resume from persisted state regardless of
how many recent rows happen to exist.

## Ledger for non-success observations (`id_status`)

`torrents` holds successes only. A boolean flag on that table would conflate a
not-yet-existing frontier ID, an internal gap, a removed torrent, and a
transient/blocked response — and a tombstone for a future ID would corrupt
`max(torrent_id)` and make the crawler skip real IDs that later appear below it.

`id_status` (PK `torrent_id`, disjoint from `torrents`):

| Column | Notes |
|---|---|
| `torrent_id` | PK |
| `status` | one of `frontier_missing`, `internal_gap`, `gone`, `transient_error`, `parse_error` |
| `attempts` | retry count |
| `last_checked_at` | |
| `next_retry_at` | drives retry scheduling |
| `last_http_status` | raw status/redirect target for debugging |

Policy:
- A soft-404 above `highest_success_id` → `frontier_missing`, retried every 5
  minutes (this ID might just not exist *yet*).
- Once a higher ID succeeds, missing IDs below it → reclassified `internal_gap`
  (`db.reclassify_internal_gaps`), retried roughly every 30 days.
- Redirects are **not** auto-followed (`allow_redirects=False`) → classified `gone`.
- A `200` that doesn't match the expected detail-page structure (missing
  `.detailsdescr`, no magnet anchor, etc.) is `parse_error`, not success — this
  also catches block/challenge pages that happen to return `200` but aren't
  positively identified as an interstitial.
- `blocked`/`server_error`/`timeout`/`connection_error` → `transient_error`,
  retried after 15 minutes.

## Rate limiting and host pool (`host_pool.py`)

Pacing is a fixed per-domain interval with one in-flight request per host — not
two independent knobs (a global rate limiter plus a separate concurrency
ceiling). Concurrency **is** the host count: `len(BASE_URLS)`.

```text
Host
  base_url                 # https://xxxclub.to
  next_start_at            # monotonic deadline; the only pacing state
  current_block_cooldown   # doubles on consecutive blocks, resets on any non-block response
```

Each `Host` sits in an `asyncio.Queue` exactly once. That single fact provides,
with no locks and no selection logic:

- atomic host selection (`Queue.get()` is atomic),
- at most one in-flight request per domain, so total concurrency is `len(hosts)`,
- fair distribution, since hosts are re-enqueued as they finish,
- a cooling host that is genuinely absent from circulation rather than skipped over,
- callers that simply wait when every host is cooling.

`HostPool` exposes one method, `fetch(path, validate, is_blocked)`. There is no
public `acquire()`/`release()` pair, so a caller cannot leak a slot. Sequence:
pull a host from the queue, sleep until `next_start_at` if needed, set the next
deadline, perform the request, compute a cooldown from the outcome, and always
(even on cancellation) return the host to the queue — immediately if the
cooldown is zero, or via `loop.call_later(cooldown, ...)` if not. The `finally`
block is the entire cancellation story: a cancelled `fetch()` still returns its
host.

`_request()` converts every expected transport result into an `Outcome` value —
timeouts and connection errors are caught here, not left to propagate, because if
they escaped `fetch()`'s try block the `finally` would return the host with zero
cooldown and the 5xx/timeout backoff below would silently not apply, in exactly
the overload case that needs it most. Only `CancelledError` and genuine
programmer errors escape `fetch()`.

Fractional rates need no special handling: the interval is `1 / rate`, which is
2.0s at `0.5`. Total concurrency equals the number of hosts (typically 1–3);
measured response time is 0.14–0.48s, so one slot per host saturates somewhere
around 2–7 rps per domain — comfortable headroom over the configured 1 rps.
Pushing a domain's configured rate past that ceiling would need a second queue
slot per host, which is not a free extension: two consumers sharing one
`next_start_at` would both read it, both find it in the past, and start
simultaneously. That would need a per-host pacing lock held across the
read-sleep-write — a design change to work through if ever needed, not a config
knob, and not needed at current rates.

Response policy:

| Outcome | Ledger | Host |
|---|---|---|
| `200`, valid structure | success | no cooldown |
| `200` soft-404 (`errordiv`) | `frontier_missing` / `internal_gap` | no cooldown |
| `404` (not observed in practice) | `frontier_missing` / `internal_gap` | no cooldown |
| Redirect | `gone` | no cooldown |
| `403`, `429`, positively identified interstitial | `transient_error` | cool |
| `Retry-After` present | as above | cool for `Retry-After`, capped at 15 min |
| `5xx`, timeout, connection error | `transient_error` | cool briefly (5s) |
| Parse failure on a structurally valid page | `parse_error` | no cooldown |
| `200`, unrecognized (none of the above) | `parse_error` (or `not_found` if it matches the soft-404 pattern) | no cooldown |

**There is no real 404.** Verified live against `/details/460000` and
`/details/999999`: the site returns HTTP `200` carrying its normal navigation
plus `<div class="errordiv"><h1>Error :</h1> … 404 : Not Found`. There is no
status code to key on, so `looks_like_soft_404()` matches on that body text, and
this is the *normal* outcome for any ID that doesn't exist yet — routine frontier
probing above the current max produces a stream of these and must not be treated
as a block.

A block must be **positively identified** (`looks_like_challenge()` in
`scraper.py`, matching interstitial-only markers). A `200` that is merely
unrecognized is left as `unrecognized`/`parse_error` and never cools a host —
treating "I don't recognize this" as "I am being blocked" would convert any
unfamiliar page into a throttle. Cloudflare's beacon script
(`/cdn-cgi/challenge-platform/scripts/jsd/main.js`) is injected into every page,
valid ones included, so `CHALLENGE_MARKERS` never matches on the bare word
"challenge".

`5xx`/timeout cools the host briefly (`BRIEF_COOLDOWN`, a few seconds) as the
origin-overload signal. Since all three domains share one origin, a sick origin
isn't a property of one hostname — cooling only the host that saw the error means
the other two discover it independently, on their own next request. Convergence
takes up to three requests, which is fine and simpler than a shared health
signal.

`Retry-After` is normalized in `_normalize_retry_after`: absent, unparseable,
non-finite (`nan`/`inf`), or `<= 0` (reachable via the HTTP-date form on a past
date or clock skew) falls back to the host's `current_block_cooldown`; whatever
survives is capped at 15 minutes (`COOLDOWN_CAP`). A hostile or broken header
must not park a host for the rest of the day, and a negative one must not
schedule a return in the past.

**Escalation**: consecutive block responses double a host's
`current_block_cooldown`, capped at 15 min; any non-block response resets it to
`BLOCK_COOLDOWN_SECONDS`. This resets on success, unlike a naive exponential
breaker whose trip count never resets for the process lifetime.

**All-hosts-cooling logging**: tracked via a `cooling_host_count` counter, logged
once on the transition to `cooling_host_count == len(hosts)` (with a matching
line when it drops back) — not on `Queue.empty()`, which is the normal state
whenever every host is simply busy and would otherwise produce one log line per
waiting caller in a large batch.

The same ID is never fetched from two hosts at once, and a blocked ID is never
retried on a different host — each ID is enqueued once by the caller, `fetch()`
takes exactly one host, and retries go through the ledger.

## Frontier discovery (forward)

`/torrents/topten/` is a **lower bound** on the true max, used only once at
bootstrap to seed `frontier_scan_high` — not re-polled as an oracle on every
cycle. `_discover_topten_max()` goes through the pool like every other request,
so discovery is paced and cooled on the same terms as the crawl itself.

Each cycle:
- Anchor = `max(highest_success_id, max(torrent_id) actually in the torrents table)` —
  folding in successes found by other cycles (e.g. the retry ledger) so the cap
  tracks real growth, not just what the frontier cycle itself has seen.
- Probe a lookahead window above `frontier_scan_high` (`FRONTIER_LOOKAHEAD`,
  default 500), but never more than `FRONTIER_DRY_STREAK` (default 200) IDs past
  the newest real torrent — everything above that is a guaranteed miss until the
  site publishes more, so marching further just burns request budget on
  non-existent future IDs.
- Every ID in the window gets resolved (success → `torrents`, else →
  `id_status`), no skipping.
- If the window is fully dry (`start > end`), the cycle logs and holds rather
  than scanning further; the retry ledger re-checks the `frontier_missing` gap
  every 5 minutes to catch new uploads instead.

## Backward walk (bootstrap / bounded backfill)

No binary search — gaps make "does this ID exist" non-monotonic against date.

- `backward_drain_chunk` walks a bounded batch (`BACKFILL_CHUNK_SIZE`, default
  500) descending from `contiguous_floor_id`, resolving every ID (success or
  ledger) — same no-skipping rule as forward.
- After each chunk, `db.confirm_window_all_old` checks whether the last
  `BACKFILL_CONFIRM_WINDOW` (default 100) resolved IDs are all older than
  `backfill_cutoff_at`. If so and no anchor is set yet, the current floor becomes
  `backfill_confirm_anchor_id`. If a newer row shows up in the window before the
  safety margin is met, the anchor resets to null.
- Once the anchor has held for `BACKFILL_SAFETY_MARGIN_IDS` (default 200) more
  IDs, `backfill_completed_at` is set and the backward walk stops.
- This is high-confidence bounded backfill, not a guaranteed-complete-by-date
  backfill. A newly discovered high ID is always ingested regardless of its
  Added Date — date is a stopping heuristic for the *old* end, never a filter on
  the *new* end.
- Raising `BACKFILL_DAYS` later moves `backfill_cutoff_at` further back
  (`bootstrap_if_needed` detects `desired_cutoff < state["backfill_cutoff_at"]`)
  and clears `backfill_completed_at`/`backfill_confirm_anchor_id`, resuming the
  downward walk from `contiguous_floor_id` rather than restarting from scratch.

`BACKFILL_DAYS`: `.env` sets `2` for a quick first run; code default is `30`.

## Unified crawler loop (`crawler.py`)

Single process, single event loop, one shared `HostPool` — avoids needing
cross-process locking to stop a backfill loop and a watcher loop from fighting
over the same frontier IDs. Each cycle (`run_cycle`) runs, in order:

1. **Retry due ledger** — `id_status` rows whose `next_retry_at` has passed.
2. **Frontier scan/extend** — forward discovery, above.
3. **Backward drain chunk** — bounded batch of the backward walk, only while
   `backfill_completed_at IS NULL`, so it never hogs a full cycle and coexists
   with frontier watching.
4. **Age-tiered refresh** — rows where `next_refresh_at <= now()`, batch-limited,
   re-scraped, `next_refresh_at` recomputed per tier:

   | Row age | Refresh cadence |
   |---|---|
   | < 7d | every 6h (covers new torrents too, no separate <24h tier) |
   | < 30d | daily |
   | older | weekly |

   A row that resolves to anything other than `success`/`not_found`/`redirect`
   (i.e. a transient failure) gets its `next_refresh_at` bumped forward 6h rather
   than left stuck at a past due time.

`run_forever()` runs `bootstrap_if_needed()` once, then loops `run_cycle()` every
`CYCLE_INTERVAL` seconds, logging and continuing past any exception from a single
cycle rather than crashing the process.

No Postgres advisory lock is taken — the single-process design makes cross-process
coordination unnecessary; add one deliberately if a second crawler instance is
ever run.

## Data model (Postgres)

`torrents` — successes only, keyed by `torrent_id`:

| Column | Type | Notes |
|---|---|---|
| `torrent_id` | bigint PK | site's numeric ID |
| `info_hash` | text, indexed (not unique) | a repost can legitimately reuse a payload hash under a different `torrent_id` — no 1:1 assumption. `CHECK` constraint for 40-hex-char shape. |
| `title` | text | |
| `category` | text | e.g. "720p/HD" — no separate `category_id`, joinable off this text (~6 fixed values) |
| `size_bytes` | bigint | parsed; no separate `size_text` column |
| `added_at` | timestamptz, not null | parsed from "Added Date", localized via `SITE_TZ` (default `UTC`, documented assumption since the markup carries no timezone). Always present — a page where it fails to parse is `parse_error`, not a success with a null date. |
| `seeders` / `leechers` | int | |
| `last_scraped` | timestamptz, nullable | `NULL` whenever the site shows "Pending" or anything not a parseable date |
| `uploader` | text | |
| `downloads` | int | |
| `tags` | text[] | display text only, no href |
| `magnet` | text | verbatim from site |
| `image_url` | text | main poster |
| `images` | text[] | high-res URLs extracted from the description at parse time, `/1s/` → `/1/` |
| `description_text` | text | `.description` fragment minus screenshots/MediaInfo, plain text |
| `media_info` | jsonb | parsed from `.description`'s monospace `<font>` block: top-level keys (`File name`, `File size`, `Duration`) plus nested `Video`/`Audio`/etc section dicts; `null` if the release has no MediaInfo block |
| `files` | jsonb | `[{filename, size_text}]` |
| `likes` / `dislikes` | int | |
| `first_seen_at` | timestamptz | when first scraped |
| `scraped_at` | timestamptz | last (re)scrape |
| `next_refresh_at` | timestamptz | drives age-tiered refresh scheduling |

`id_status` and `crawl_state` — see above.

Known limitation, not being solved: image URLs point at third-party hosts
(`imgxclub.com`, `imgtraffic.com`). If "rebuild the page" must survive those
hosts deleting content, URLs alone aren't enough — would need to mirror images to
local/object storage. Flagged as a known gap, out of scope unless wanted later.

## Config (`config.py`, `.env`)

```ini
POSTGRES_USER=xxxclub
POSTGRES_PASSWORD=xxxclub
POSTGRES_DB=xxxclub
POSTGRES_HOST_PORT=5432
XXXCLUB_IMAGE=xxxclub:local        # shared image for scraper/web/tpdb-matcher

BASE_URLS=https://xxxclub.to       # comma-separated; single fallback var is BASE_URL
SITE_TZ=UTC
BACKFILL_DAYS=2                    # code default 30 if unset

REQUESTS_PER_SECOND_PER_DOMAIN=1   # 0.5 = one request every two seconds; min 0.01
BLOCK_COOLDOWN_SECONDS=60          # Retry-After overrides this when usable; both capped at 15 min
REQUEST_TIMEOUT_SECONDS=15

FRONTIER_LOOKAHEAD=500
FRONTIER_DRY_STREAK=200
BACKFILL_CHUNK_SIZE=500
BACKFILL_CONFIRM_WINDOW=100
BACKFILL_SAFETY_MARGIN_IDS=200
RETRY_BATCH_SIZE=200
REFRESH_BATCH_SIZE=200
CYCLE_INTERVAL=60

THEPORNDB_API_KEY=
TPDB_REQUESTS_PER_SECOND=1
TPDB_BATCH_SIZE=25
TPDB_CYCLE_INTERVAL=60
```

`load_config()` is defensive, not adaptive — it rejects values that cannot work
rather than tuning anything at runtime:

- `check_removed_keys()` raises by name if any of `MAX_CONCURRENCY`,
  `MAX_CONCURRENCY_CEILING`, `MAX_REQUESTS_PER_SECOND`,
  `MAX_REQUESTS_PER_SECOND_CEILING` are set — these knobs don't exist in the
  host-pool design, and silently ignoring them would let a deliberately-tuned
  rate become a new default without a word in the log.
- `REQUESTS_PER_SECOND_PER_DOMAIN`, `BLOCK_COOLDOWN_SECONDS`,
  `REQUEST_TIMEOUT_SECONDS` must be finite (`math.isfinite` — `float()` happily
  parses `inf`/`nan`) and strictly positive; the rate additionally floors at
  `0.01`.
- `BASE_URLS` is split on commas, each entry stripped and normalized
  (`normalize_base_url`): must be a bare `http`/`https` origin — no path, query,
  fragment, or embedded credentials — and duplicates are rejected (a duplicate
  would silently double that domain's concurrency and rate while the log still
  reports the intended per-domain figure). At least one valid entry is required.
- `log_config()` masks `POSTGRES_PASSWORD` and logs the derived pacing line,
  e.g. `pacing: 1 request every 1.00s per domain across 3 domain(s) = 3.00 req/s total`.

`entrypoint.py` logs config with secrets masked and never dumps the full
environment on startup.

## Probe methodology (`probe_limit.py`)

Before enabling more than one `BASE_URLS` entry, run `python3 probe_limit.py
--compare` with the scraper stopped. Three steps, 120s each, with a recovery
check between them:

1. **1 rps single-host baseline**
2. **3 rps single-host control** — establishes whether the origin itself
   tolerates 3 rps regardless of routing
3. **3×1 rps fan-out across all three hosts**

The control matters because both a working fan-out *and* an origin that's simply
fine at 3 rps produce a clean fan-out result — they imply different available
headroom:

| Control (3 rps, one host) | Fan-out (3×1 rps) | Conclusion |
|---|---|---|
| fails | clean | Zone-level limit — the fan-out is doing real work. Ship three hosts at 1 rps. |
| clean | clean | Origin tolerates 3 rps by any routing. Ship three hosts at 1 rps, headroom to raise later. |
| fails | fails | 3 rps total is too much however it's routed. Ship three hosts at a reduced per-domain rate (e.g. 0.33), then probe upward. |
| clean | fails | `.cc`/`.me` are tuned tighter than `.to`. Reduce the rate, or drop the tighter hosts if a lower rate isn't worth it. |

Every outcome still ships all three hosts; the bottom two rows just mean the 3x
throughput goal isn't reachable at 1 rps per domain, which is information about
the site rather than a reason to abandon multi-domain.

## TPDB catalog extension

The read-only web UI additionally exposes archives and detail pages for scenes,
sites, networks, and performers, populated only from successful TPDB scene
matches (normalized foreign keys plus a scene-performer join table). This
extension is additive (`CREATE TABLE IF NOT EXISTS`) and runs as its own Compose
service (`tpdb-matcher`) with its own request-rate setting — it must not use or
mutate the crawler's `HostPool` or `crawl_state`.

Matching flow (`tpdb_matcher.py`, `tpdb_client.py`):

1. Eligible categories: `1080p/FullHD`, `2160p/UHD/4K`, `480p/SD`, `720p/HD`,
   `VR/VirtualReality`.
2. Parse comma-formatted sizes and prefer the largest recognized video file
   (`largest_file`).
3. Normalize resolution variants into a scene key (`scene_key`) and reuse
   verified sibling matches before making an API request.
4. Search by parsed filename, the resolved site's recent scenes, then cleaned
   text queries (`search_with_fallbacks`).
5. Score candidates on site, release date, title, and performer evidence
   (`choose_candidate`/`_candidate_score`); reject ambiguous or stale candidates.
6. Persist method/query/score/candidate audit data per outcome
   (`tpdb_match_attempts`) and retry unmatched results with increasing backoff
   (`unmatched_retry_at`).

`--dry-run` (`tpdb_matcher.py`) evaluates one batch without persisting match
outcomes — useful for checking scoring changes before they write to the DB.

## Web UI (`webapp.py`)

Routes:

- `GET /` — catalog archive (default landing page)
- `GET /torrents`, `/torrents/` — torrent archive
- `GET /torrent/{torrent_id}` — torrent detail
- `GET /scenes`, `/scenes/` — redirects into the catalog archive
- `GET /tags` — tag index
- `GET /{entity:scenes|sites|networks|performers}` — catalog archive per entity type
- `GET /{entity:scenes|sites|networks|performers}/{entity_id}` — catalog detail

Templates live in `web/templates/`. The service shares the `db.py` schema module
and connects to the same Postgres instance as the scraper and matcher, but only
reads.

## Docker layout

`docker-compose.yml` (and `docker-compose.ghcr.yml`) define `db` (Postgres),
`scraper`, `web`, and `tpdb-matcher`, all built from one shared image
(`XXXCLUB_IMAGE`). `Dockerfile`'s `ENTRYPOINT` is `xxxclub`, dispatching on the
`command:` argument (`scraper` / `web` / `tpdb-matcher`) each service passes.

## Fixtures for hardening the parser

Only one saved fixture (`450462.html`) exists. Parsing is defensive and logs
anomalies for later capture rather than assuming full coverage. Useful examples
to capture via the live crawl's own logging, if the parser needs further
hardening:

- Dated **and** "Pending" Last Scraped
- Missing poster / collection / description / file list
- Multiple tags, many files
- A genuinely removed/gone torrent vs. a redirect vs. a soft-404
- A soft-404 or challenge/block page (to validate the structure-check catches it)
- An existing torrent whose mutable fields (seeders/downloads/likes) changed
  between two scrapes

## Tests

Offline, fake clock, no network or DB:

- `fetch()` at rate `0.5` returns under a timeout.
- Ten sequential fetches at `0.5` span ≥18s of monotonic time; at `1`, ≥9s.
- Three hosts, nine fetches → 3/3/3 distribution.
- Never more than one in-flight per host, never more than `len(hosts)` in total.
- A cooling host is not handed to any caller until its cooldown elapses.
- All three cooling → callers wait, no spin, no request escapes.
- A cancelled `fetch()` returns its host; repeat 3x and confirm the pool still works.
- A parse failure on a structurally valid page does not cool the host.
- A soft-404 does not cool the host.
- A timeout and a connection error each cool the host (i.e. `_request()`
  converted the exception into an `Outcome` rather than letting it skip
  `_cooldown_for()`).
- `Retry-After: 99999` caps at 15 min; a past HTTP-date, `nan`, and a malformed
  value each fall back to the configured cooldown rather than scheduling in the
  past.
- Consecutive blocks double the cooldown; any non-block response resets it.
- All-hosts-cooling logs once on transition, not once per blocked caller: three
  hosts cooling with many queued callers produces exactly one line.
- Startup raises on each removed config key, on rate `0`/`inf`/`nan`, on a
  non-positive cooldown or timeout, and on a `BASE_URLS` containing a duplicate
  host, an empty entry, a non-HTTP(S) scheme, credentials, or a path.

Test files: `test_config.py`, `test_host_pool.py`, `test_page_classification.py`,
`test_tpdb_client.py`. Run with:

```bash
python3 -m unittest discover -p "test_*.py"
```
