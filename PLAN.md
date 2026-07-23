# xxxclub V2 — plan (rev 2, post-review)

Codex review found the numeric-ID approach sound but the original crawl-state, 404-handling, rate-limit, and retention design were not implementation-ready. This revision addresses all four non-negotiables it raised. Evidence from the review (re-checked live, after plan-writing time): `/torrents/details/450463` was a 404 at plan-writing time and is a real page now; `/torrents/topten/` showed max `450298` and `/torrents/browse/all/` showed max `450289` while `450462`/`450463` already existed; ID `1`'s Added Date reads `21 May 2020`, not the `23 Jun 2020` originally assumed. None of these break the approach, but they kill any design that treats topten-max, "several 404s", or date monotonicity as trustworthy signals on their own.

## Core insight vs V1 (unchanged)

V1 scraped `/torrents/browse/all/` — hash IDs, fake pagination decoys, title-matching hacks for top100.

V2 uses `/torrents/details/{id}` — sequential numeric ID, no pagination needed. Backfill/frontier-tracking = walk IDs, not pages. No decoy links, no hash-matching.

Correction: don't hardcode "ID=1 is 23 Jun 2020" or treat ID↔date as strictly monotonic anywhere in code. Treat it as approximately true, sufficient for a bounded ~30-day backfill, not as a proof of range completeness.

## What's on one details page (confirmed from saved 450462.html)

- `h1` title
- Category (text + numeric id from `/torrents/browse/{n}/` href) — store both, not just text
- Size (text, e.g. "532.59 MB")
- Added Date (e.g. "23 Jul 2026 13:46:16") — no timezone in the markup; see Data model
- Peers: `<font class="see">` seeders, `<font class="lee">` leechers
- Last Scraped — dated timestamp, or "Pending", or potentially absent/unexpected — tri-state, see Data model
- Uploader
- Downloads (count)
- Collection/tags — one or more `<li>` rows, link text only kept (href/slug dropped)
- Torrent download link: `/torrents/download/{40-char-hash}` ← real SHA1 payload hash
- Magnet URI — fully built site-side with trackers, correct `:` encoding
- `og:image` / `.detailsposter` src — main poster image
- Description block (`.description`) — screenshot `<a><img>` markup stripped (URLs already extracted into `images`), MediaInfo `<font>` block stripped and parsed into `media_info`, remainder (release-name line etc.) kept as plain text (`<br>` → `\n`) in `description_text` (see Data model / Retention decision)
- Files list (`.filestable`) — `<li>` pairs of filename + size
- Likes / Dislikes (`.rating-system`)
- "Similar Torrents" (`.similardiv`) — skip, not stored. Parsing must be scoped to `.detailsdescr`/`.rating-system`/`.filestable`/`.description` specifically so this sidebar can never bleed into main-page fields.

Meta tags are partial fallbacks only — they don't restate uploader, peers, ratings, files, magnet, or description.

## Non-negotiable 1 — persisted crawl state (not inferred from `torrents` contents)

Original flaw: "does a recent row exist?" can't tell a completed backfill from a crashed one-row bootstrap, and one old row isn't evidence the cutoff was fully scanned.

New table `crawl_state` (single row, or one row per named counter):

| Field | Purpose |
|---|---|
| `highest_success_id` | highest `torrent_id` ever successfully parsed |
| `contiguous_floor_id` | lowest `torrent_id` such that **every** ID from here up to `highest_success_id` is resolved — either a `torrents` row or an `id_status` ledger row. This is the real "have we covered this range" watermark, not a date check. |
| `backfill_cutoff_at` | the date boundary backfill is walking down toward (`now() - BACKFILL_DAYS`, fixed at bootstrap start so raising `BACKFILL_DAYS` mid-run doesn't move the goalposts under a live run) |
| `backfill_completed_at` | null while in progress; set once the confirm-window rule below is satisfied |
| `frontier_scan_high` | highest ID a forward probe has reached, so cycles resume the lookahead window instead of restarting from `highest_success_id + 1` every time |

Rule: `contiguous_floor_id` only moves down when every single ID in the gap has a resolution (success or ledger entry) — no skipping. This makes "increase `BACKFILL_DAYS` later" naturally extend the walk downward from wherever it stopped, and makes an interrupted run resume from persisted state regardless of how many recent rows happen to exist.

## Non-negotiable 2 — separate ledger for non-success observations

`torrents` holds successes only. A `not_found` boolean on that table was wrong — it conflates a not-yet-existing frontier ID, an internal gap, a removed torrent, and a transient/blocked response, and (worse) a tombstone for a future ID would corrupt `max(torrent_id)` and make the watcher skip real IDs that later appear below it.

New table `id_status` (PK `torrent_id`, disjoint from `torrents`):

| Column | Notes |
|---|---|
| `torrent_id` | PK |
| `status` | one of `frontier_missing`, `internal_gap`, `gone`, `transient_error`, `parse_error` |
| `attempts` | retry count |
| `last_checked_at` | |
| `next_retry_at` | drives retry scheduling |
| `last_http_status` | raw status/redirect target for debugging |

Policy:
- A 404 above `highest_success_id` → `frontier_missing`, retried with increasing backoff (this ID might just not exist *yet*).
- Once a higher ID succeeds, missing IDs below it → reclassified `internal_gap`.
- `internal_gap` rows stop getting frequent retries after repeated 404s + a grace period, but keep a very infrequent audit retry (e.g. monthly) rather than being permanently skipped.
- Redirects are **not** auto-followed (`allow_redirects=False`) — inspect the `Location` and classify explicitly.
- A `200` that doesn't match the expected detail-page structure (missing `.detailsdescr`, no magnet anchor, etc.) is `parse_error`, not success — this also catches block/challenge pages that return `200`.

## Non-negotiable 3 — rate limiting is not the same as concurrency

Original flaw: 8 concurrent workers with 100–300ms jitter each is not a rate limit, it's ~8x whatever one worker does. V1's own serial delay (0.5–1.5s) and retry logic aren't evidence the site tolerates 8 concurrent clients.

Two independent knobs:
- `MAX_CONCURRENCY` — ceiling on simultaneous in-flight requests.
- `MAX_REQUESTS_PER_SECOND` — global token-bucket shared across *all* workers, independent of how many workers exist.

Start conservative, ramp up on evidence:
- Initial defaults: `MAX_CONCURRENCY=2`, `MAX_REQUESTS_PER_SECOND=1`.
- After a sustained clean window (config: N requests / T minutes with zero anomalies), step concurrency up by 1 and raise the rate-limit token refill proportionally, capped at an absolute ceiling (`MAX_CONCURRENCY` config max, e.g. 8; `MAX_REQUESTS_PER_SECOND` config max, e.g. 4) regardless of concurrency.
- **Circuit breaker**: on `403`, `429`, `5xx`, timeout, connection error, challenge-page markers, abnormal latency spike, or `parse_error` rate spike → trip breaker, drop concurrency/rate back to floor, cooldown (exponential on repeat trips), then ramp up again slowly. A block page must not get multiplied across 8 workers before anyone notices.
- Respect `Retry-After` when present.
- Plain `404` is **not** an error signal for the breaker — it's a normal, expected data outcome (see ledger above), don't conflate "the torrent doesn't exist" with "we're getting blocked".

## Retention decision — description kept as plain text, media info structured, raw HTML dropped

Review recommended keeping both `description_html` and `raw_html`. Settled: `raw_html` (full page) stays dropped — not needed. The `.description` fragment is kept, but not verbatim — it's HTML that reduces to three parts, each better stored differently: screenshot thumbnails (already redundant with `images`), a MediaInfo block (structured key/value data, not prose), and a free-text remainder (release-name line, occasionally more). Splitting these out means `description_text` is genuinely plain text (no leftover markup to strip on read) and `media_info` (duration/codec/resolution/bitrate/channels) is queryable without re-parsing HTML.

- `description_html` — **dropped**, superseded by `description_text` + `media_info`
- `description_text` — `.description` fragment with screenshot markup and the MediaInfo block removed, `<br>` → `\n`, plain text
- `media_info` — jsonb, parsed from the `.description`'s monospace `<font>` block (see Data model); `null` when the release has no MediaInfo block (image-only releases)
- `raw_html` — **not stored**
- `images` — `text[]`, transformed high-res URLs, extracted from the description at parse time (see Data model) — stored as its own column, since querying/filtering by image shouldn't require parsing HTML back out.

## Data model (Postgres)

`torrents` — successes only, keyed by `torrent_id`:

| Column | Type | Notes |
|---|---|---|
| `torrent_id` | int PK | site's numeric ID |
| `info_hash` | text, indexed (not unique) | a repost can legitimately reuse a payload hash under a different `torrent_id` — don't assume 1:1. Add a `CHECK` constraint for 40-hex-char shape instead of `char(40)`. |
| `title` | text | |
| `category` | text | e.g. "720p/HD" — no separate `category_id`, computable/joinable off this text (~6 fixed values) |
| `size_bytes` | bigint | parsed; no separate `size_text` column, format on read if display text is ever needed |
| `added_at` | timestamptz, not null | parsed from the "Added Date" field, localized via `SITE_TZ` config (default `UTC` — the markup carries no timezone, this is a documented assumption, not a silent one). Always present — unlike Last Scraped, the site never shows this as "Pending"/missing, so no raw-text fallback column is kept; a page where it fails to parse is treated as `parse_error`, not a success with a null date. |
| `seeders` | int | |
| `leechers` | int | |
| `last_scraped` | timestamptz, nullable | single field, no raw/tri-state split — `NULL` whenever the site shows `"Pending"` or anything not a parseable date |
| `uploader` | text | |
| `downloads` | int | |
| `tags` | text[] | tag display text only, no href |
| `magnet` | text | verbatim from site |
| `image_url` | text | main poster |
| `images` | text[] | high-res URLs extracted from the description at parse time, `/1s/` → `/1/` |
| `description_text` | text | `.description` fragment minus screenshots/MediaInfo, plain text — see Retention decision above |
| `media_info` | jsonb | parsed from `.description`'s monospace `<font>` block: top-level keys (`File name`, `File size`, `Duration`) plus nested `Video`/`Audio`/etc section dicts; `null` if the release has no MediaInfo block |
| `files` | jsonb | `[{filename, size_text}]` |
| `likes` | int | |
| `dislikes` | int | |
| `first_seen_at` | timestamptz | when we first scraped it |
| `scraped_at` | timestamptz | last (re)scrape |
| `next_refresh_at` | timestamptz | drives age-tiered refresh scheduling, see below |

`id_status` — see Non-negotiable 2.

`crawl_state` — see Non-negotiable 1.

Known limitation, not being solved now: image URLs point at third-party hosts (`imgxclub.com`, `imgtraffic.com`) — if "rebuild the page" must survive those hosts deleting content, URLs alone aren't enough; would need to mirror images to local/object storage. Flagging as a known gap, not building it — out of scope unless you want it.

## Frontier discovery (forward)

`/torrents/topten/` and `/torrents/browse/all/` are **lower bounds**, not current-max oracles — the review found both lagging the true max by 150+ IDs at check time. Design accordingly:

- Anchor = `max(highest_success_id, highest ID seen across topten/browse discovery pages)`.
- Each cycle, probe a lookahead window above `frontier_scan_high` (config `FRONTIER_LOOKAHEAD`, e.g. 500 — generous, since real gaps here are removed/rejected torrents, likely sparse, and the observed 150+ ID lag means a small lookahead would falsely declare "frontier reached" mid-gap).
- Every ID in the window gets resolved (success → `torrents`, else → `id_status`), no skipping.
- The window only "closes" (stop treating it as active frontier, though still eligible for retry per ledger policy) after a **dry streak** — `FRONTIER_DRY_STREAK` (e.g. 200) consecutive non-successes — not just "several". If a success shows up near the edge, extend the window forward and keep going.
- Every subsequent watcher cycle re-probes above `highest_success_id` again (bounded lookahead), since a 404'd frontier ID today may be real tomorrow (confirmed behavior: `450463`).

## Backward walk (bootstrap / bounded backfill)

No binary search — gaps make "does this ID exist" non-monotonic against date, so binary search over the date boundary is invalid (confirmed: dates are only approximately monotonic, not guaranteed).

- Sequential/concurrent descending walk from the frontier anchor, resolving every ID (success or ledger) — same "no skipping" rule as forward.
- Stop only when **all** of:
  - a confirm window (`BACKFILL_CONFIRM_WINDOW`, e.g. 100 resolved IDs) has every `torrents.added_at` older than `crawl_state.backfill_cutoff_at`, and
  - the walk has extended at least `BACKFILL_SAFETY_MARGIN_IDS` past the first old row (ID-based safety margin, since date isn't fully trustworthy), and
  - every ID scheduled above that boundary has finished or is recorded as a retryable ledger hole (not silently dropped).
- On satisfying all three: set `crawl_state.backfill_completed_at`, `contiguous_floor_id` = the confirmed boundary.
- This is documented as **high-confidence bounded backfill**, not a guaranteed-complete-by-date backfill. A newly discovered high ID is always ingested regardless of its Added Date — date is a stopping heuristic for the *old* end, never a filter on the *new* end.
- Raising `BACKFILL_DAYS` later moves `backfill_cutoff_at` further back and resumes the downward walk from `contiguous_floor_id` — it does not restart from scratch (ties into Non-negotiable 1).

`BACKFILL_DAYS`: `.env` sets `2` for the initial run; code default `30` if unset.

## Unified crawler loop (replaces separate backfill.py/watcher.py racing each other)

Single process, single event loop, one shared rate limiter/concurrency pool — avoids needing cross-process locking to stop backfill and watcher from fighting over the same frontier IDs. Each cycle runs, in order:

1. **Frontier scan/extend** (forward discovery, above)
2. **Backward drain chunk** — a bounded batch of the backward walk (only while `backfill_completed_at IS NULL`), so it never hogs a full cycle and coexists with frontier watching
3. **Age-tiered refresh** — pull rows where `next_refresh_at <= now()`, limit batch size, re-scrape, recompute `next_refresh_at` per tier:

   | Row age | Refresh cadence |
   |---|---|
   | < 7d | every 6h (covers new torrents too, no separate <24h tier) |
   | < 30d | ~daily |
   | older | ~weekly or off (config) |

   This replaces the original flat "refresh most-recent-N" (which permanently starves everything older than N) with a TTL that still eventually revisits older rows.

If a Postgres advisory lock is ever needed (e.g. running more than one crawler instance), take it around the whole cycle — but the single-process design above should make that unnecessary for now.

## Fixtures needed before hardening the parser

Only one saved fixture (`450462.html`) exists. Parsing should be defensive and log-anomalies-for-later-capture rather than assume full coverage. Before considering the parser done, capture (via the low-rate crawl itself, logging anomalies) examples of:

- Dated **and** `"Pending"` Last Scraped
- Missing poster / collection / description / file list
- Multiple tags, many files
- A genuinely removed/gone torrent vs. a redirect vs. a plain 404
- A `200` soft-404 or challenge/block page (to validate the structure-check catches it)
- An existing torrent whose mutable fields (seeders/downloads/likes) changed between two scrapes

## Docker layout

`docker-compose.yml` with `db` (postgres) + `scraper` service. `.env`: `POSTGRES_*`, `BASE_URL`, `SITE_TZ` (default `UTC`), `BACKFILL_DAYS` (default 30, set `2` initially), `MAX_CONCURRENCY` (default 2, ceiling e.g. 8), `MAX_REQUESTS_PER_SECOND` (default 1, ceiling e.g. 4), `FRONTIER_LOOKAHEAD`, `FRONTIER_DRY_STREAK`, `BACKFILL_CONFIRM_WINDOW`, `BACKFILL_SAFETY_MARGIN_IDS`, `CYCLE_INTERVAL`.

Entrypoint must **not** dump the full environment on startup (V1's `entrypoint.py` logs every env var including DB password/API keys — not carrying that bug forward). Log which features/config are active with secrets masked.

## Build order

1. `db.py` — schema: `torrents`, `id_status`, `crawl_state`; upsert helpers
2. `rate_limiter.py` — token bucket + concurrency semaphore + circuit breaker, independent of scraper logic
3. `scraper.py` — fetch (no auto-redirect) + parse single details page → dict, scoped to the specific containers, structure-validated (200-but-wrong-shape → `parse_error`); test against saved `450462.html` first, no network needed
4. `crawler.py` — unified loop: frontier scan, backward drain chunk, age-tiered refresh, all through the shared rate limiter
5. `entrypoint.py` — startup, masked config logging, runs `crawler.py` loop
6. `Dockerfile` + `docker-compose.yml` + `.env.example`

Original scope: scraper + DB only. The later TPDB catalog extension is documented
below rather than folded into crawler state or rate limiting.

## TPDB catalog extension

The read-only web UI exposes archives and detail pages for scenes, sites,
networks, and performers. These entities are populated only from successful
TPDB scene matches and use normalized foreign keys plus a scene-performer join
table.

The initial no-scoring baseline was replaced after measuring its unmatched
ledger. The matcher now:

1. Eligible categories are `1080p/FullHD`, `2160p/UHD/4K`, `480p/SD`,
   `720p/HD`, and `VR/VirtualReality`.
2. Parse comma-formatted sizes and prefer the largest recognized video file.
3. Normalize resolution variants into a scene key and reuse verified sibling
   matches before making an API request.
4. Search by parsed filename, resolved site's recent scenes, then cleaned text
   queries.
5. Score multiple candidates using site, release date, title, and performer
   evidence; reject ambiguous or stale candidates.
6. Persist method/query/score/candidate audit data for every outcome and retry
   unmatched results with increasing backoff.

The matcher runs as its own Compose service and has its own request-rate setting.
It must not use or mutate the xxxclub crawler's adaptive limiter or crawl state.
`--dry-run` evaluates one batch without persisting match outcomes.
