---
name: telegram-stremio
description: 'Project skill for Telegram-Stremio, a self-hosted media server (FastAPI + MongoDB/Motor + PyroFork) that turns Telegram channels into a Stremio addon. USE WHEN working in this repo: editing Backend/fastapi/ routes (stremio, stream, api, template, webdav), Backend/helper/ modules, metadata providers (tmdb, tvdb, kitsu, cinemeta), pyrofork bot plugins, database.py, settings_manager.py, config.env, or deployment files (Dockerfile, docker-compose.yaml, Caddyfile, heroku.yml). Covers single-process architecture, boot order, stream id encoding, settings storage, ingestion pipeline, conventions, and run commands (uv run -m Backend).'
---

# Telegram-Stremio Project Skill

## What this project is

Self-hosted "Telegram Stremio" media server, version 5.x (local tree at 5.0.5). Users forward movies/episodes to Telegram channels; a PyroFork bot indexes them into MongoDB; a FastAPI app serves them as a Stremio addon (`/stremio/{token}/manifest.json`) plus a web admin panel, WebDAV, and a subscriptions bot flow. Upstream: `github.com/weebzone/Telegram-Stremio`.

**Key dependencies:** FastAPI, uvicorn (uvloop + httptools), Motor, PyroFork >= 2.3.61, themoviedb, GuessIt, parse-torrent-title, rapidfuzz, Jinja2, itsdangerous. Python >= 3.11, managed with `uv` (`pyproject.toml`).

## When to use

- Adding/editing API routes, Stremio addon endpoints, stream handlers, or admin pages.
- Touching the ingestion pipeline (receiver plugin, metadata resolvers, scan managers).
- Changing configuration (settings manager, config.env), tokens/subscriptions logic, or DB schema.
- Debugging streaming, proxying (MediaFlow), WebDAV, global search, or fanart/RPDB poster issues.
- Deploying, updating, or bumping versions.

## Architecture

Everything runs in **one asyncio event loop** in one process. No separate web worker — the Telegram bot and the uvicorn server are coroutines in the same process, sharing module-level singletons.

- `Backend/__init__.py` — shared state: `db = Database()` singleton, `__version__`, `timezone = pytz.timezone("Asia/Kolkata")`, `StartTime`, and mutable globals `USE_DEFAULT_ID`, `MANUAL_SESSION`.
- `Backend/config.py` — reads `config.env` (via load_dotenv) into class `Telegram` (`API_ID`, `API_HASH`, `BOT_TOKEN`, `DATABASE`, `PORT`, `OWNER_ID`, optional fallbacks). Env is a **first-boot seed only**; runtime settings live in MongoDB via `SettingsManager`.
- `Backend/logger.py` — `LOGGER` used everywhere (`from Backend.logger import LOGGER`); writes `log.txt` in the working directory.

### Boot order (`Backend/__main__.start_services()`)

1. `db.connect()` (all `DATABASE` URIs; DB name hardcoded `dbFyvio`)
2. `SettingsManager.initialize(db)` → adds `SessionMiddleware`
3. `scan_manager.load(db)`; bind `dbcheck_manager` / `duplicate_manager`
4. `db.reload_extra_databases(...)`
5. `StreamBot.start()` (loads `Backend/pyrofork/plugins`)
6. Userbot start (from encrypted stored session via `session_auth.py`, if present)
7. `initialize_clients()` (multi-token bots; main bot = index 0)
8. `setup_bot_commands()` → `/start`, `/set`
9. Background tasks: in-process uvicorn `server.serve()`, `ping()`, `DeadLinkChecker` (24 h)

Separately, `main.py`'s FastAPI `startup` hook spawns `decay_client_failures()` and `version_check_loop()` — those are not in `start_services()`.
10. `subscription_task_manager.sync()` → `idle()`

`stop_services()` cancels background tasks, stops `StreamBot`/`Userbot`, disconnects DB. Uvicorn config: `uvicorn.Config(app, host="0.0.0.0", port=Telegram.PORT, loop="uvloop", http="httptools")`.

## Module map

### `Backend/fastapi/`

| File | Responsibility |
|---|---|
| `main.py` | Creates `app`; mounts `/static`, Jinja templates; registers routers; ~120 `/api/*` admin endpoints guarded by `require_auth`; global 401 handler (JSON for API paths, 302 → `/login` for pages). |
| `themes.py` | 15 color themes + 3 styles; `get_theme()`, `get_all_themes()`, `get_all_styles()`; defaults `graphite_amber`/`default`. |
| `routes/stremio_routes.py` | The addon (`/stremio`): manifest, catalog, meta, subtitles. Per-token visibility filtering, poster providers, MediaFlow proxy URL building, `addon_version` expiry-epoch tag. |
| `routes/stream_routes.py` | `/dl/{token}/{id}/{name}` (GET/HEAD), `/sub/...`, `/thumb/{id}`, `/stream/stats`. Range parsing, best-client selection, 206 headers. |
| `routes/api_routes.py` | ~2500-line admin API: media CRUD, tokens, subscriptions, requests, catalogs, settings, scans, stats, logs, backup, restart. |
| `routes/template_routes.py` | Jinja pages: login, dashboard, admin, media management, settings, tools, public status/request pages. |
| `routes/webdav_routes.py` | Read-only WebDAV under `/webdav/{token}` (PROPFIND, GET/HEAD, `/refresh`). |
| `security/credentials.py` | Admin panel auth (`require_auth`, session cookie). |
| `security/tokens.py` | `verify_token()` — token validity, expiry, subscription, GB limits. Dependency on `/stremio`, `/dl`, `/sub`, `/webdav`. |

### `Backend/helper/` (core modules)

| File | Responsibility |
|---|---|
| `database.py` | The monolith: `Database` class, `connect()`, `ensure_indexes()`, media insert/update/remove, catalogs, tokens, users/plans, stats. DB name `dbFyvio`. |
| `settings_manager.py` | Runtime config source of truth. `Settings` snapshot + `SettingsManager.current()` (called at request time), `update()` hot-applies most changes. |
| `encrypt.py` | Stream id codec: `encode_string()` = base62(zlib(json)), `decode_string()` reverse. **Obfuscation, not encryption** — ids are reversible. |
| `custom_dl.py` | `ByteStreamer` — Telegram download engine: chunked prefetch, multi-client parallelism, `_prewarm_sessions()`. |
| `virtual_dl.py` | Streams split files (`.001/.002`) as one continuous stream. |
| `zip_stream.py` | Pure-Python zip reader for stored (uncompressed) archives with byte-range seeking. |
| `scan_manager.py` | `ScanManager` (channel history scan, batch 200, resumable via `tracking.scan_state`), `DbCheckManager`, `DuplicateManager`. |
| `link_checker.py` | `DeadLinkChecker` — 24 h probe of all stored files, flags dead links. |
| `manual_add.py`, `skip_channel.py`, `split_files.py` | Manual add plumbing; metadata-fail routing; split/combined part parsing. |
| `announcer.py`, `auto_catalog.py` | New-content announcements; TMDB-driven auto catalogs. |
| `subtitles.py`, `nfo_generator.py`, `fanart.py` | Subtitle ingestion/matching; Kodi NFO for WebDAV; fanart.tv artwork with TTL cache. |
| `global_search.py` | Userbot-powered on-demand search of `global_search_channels`. |
| `version_check.py` | Compares `__version__` against the upstream GitHub repo every 12 h. Module-level `_state` cache + `asyncio.Lock`; `check_upstream_version(force)`, `get_version_status()`, `version_check_loop()` (task started in `main.py`). |
| `session_auth.py` | In-app userbot login flow; stores session encoded in `tracking.state["user_session"]`. |
| `subscription_task_manager.py` | Hourly subscription expiry/kick/reminder loop. |
| `requests_manager.py`, `analytics.py`, `health.py`, `pinger.py`, `task_manager.py`, `utils.py`, `passwords.py`, `backup.py`, `modal.py`, `exceptions.py`, `custom_filter.py`, `pyro.py` | Public requests; telemetry; health checks; self-ping; bot edit/delete helpers with Userbot fallback; token usage tracking; PBKDF2 password hashing; config backup; Pydantic storage schemas; exceptions; owner filter; Telegram helpers. |

### `Backend/helper/metadata/`

Matching pipeline. **Priority chains: Anime → Kitsu > TVDB > TMDB > Cinemeta; Movies → TMDB > Cinemeta; Series → TVDB > Cinemeta > TMDB.**

- `parse.py` — `parse_media_name()` (PTN + GuessIt), absolute-episode extraction, combined pack detection.
- `entry.py` — `metadata(...)` orchestrator; builds `group_key`/`part_number`; `caption_with_id()`.
- `resolvers.py` — `resolve_movie()`, `resolve_series()`, anime variants with `default_id` override short-circuit.
- `common.py` — `cached_call()` + `API_SEMAPHORE(12)`, fuzzy scoring thresholds (`STRONG_MATCH=0.92`, provider ~0.55–0.60), `COMBINED_SEASON = 0`, `COMBINED_EPISODE_BASE = 1000`.
- `episode_maps.py` — anime absolute→S/E via Anime-Lists XML / anibridge JSON.
- `providers/` — `cinemeta.py` (keyless IMDb via Stremio Cinemeta), `tmdb.py`, `tvdb.py` (v4 token login), `kitsu.py`.

### `Backend/pyrofork/`

| File | Responsibility |
|---|---|
| `bot.py` | Client registry: `StreamBot`, `Userbot`, `multi_clients`, `work_loads`, `client_dc_map`, `client_failures`, `client_avg_mbps`. |
| `clients.py` | Multi-bot-token lifecycle, hot reload. |
| `plugins/receiver.py` | **Ingestion core**: channel file events → metadata resolution → `db.insert_media` → catalog sync + announce. Also edit/delete handlers. |
| `plugins/start.py` | `/start`: token issuance, subscription gate. |
| `plugins/subscription.py` | Purchase flow (screenshots → approver review). |
| `plugins/group_security.py` | Kicks non-subscribers joining `subscription_group_id`. |

## Key concepts

- **Streams** = `QualityDetail` entries (`quality`, `id`, `name`, `size`, optional `group_key`, `parts[]`) inside `movie.telegram[]` or `tv.seasons[].episodes[].telegram[]`. Single-file id = `encode_string({"chat_id","msg_id"})`; split id = `{"parts": [...], "zip": bool}`.
- **Catalogs** = built-in (`latest_movies`, `top_movies`, `latest_series`, `top_series`) + per-token `custom_{id}`; per-token order/hidden in token `config`; global order in `tracking.state["catalog_order"]`.
- **Tokens** = `tracking.api_tokens` docs (32-char token, expiry, GB limits, per-token `config`); embedded in every addon URL.
- **Subscriptions** = `tracking.users` + `tracking.sub_plans`; enforced in `verify_token()`, group joins, and hourly loop.
- **Ingestion**: forwarded files in `auth_channels`; bot must be admin. Movies `Title Year Quality`, TV `Title SxxExx Quality`; quality (resolution) required. Split `name.ext.NN` joined; combined packs (`E01-E04`) → season 0, episode 1000+season. Anime channels: Kitsu-first + absolute episode recovery. Manual channels attach to `Backend.MANUAL_SESSION`. After indexing, captions get stamped with IMDb/TMDb id.
- **Config**: DB-backed settings seeded once from `config.env`. Hard env-only requirements: `API_ID/API_HASH/BOT_TOKEN/DATABASE/PORT/OWNER_ID`. `DATABASE` needs **≥2 URIs** (index 0 = tracking DB, rest = storage DBs).
- **Collections** (`dbFyvio`): tracking DB — `settings`, `state`, `users`, `sub_plans`, `api_tokens`, `custom_catalogs`, `subtitles`, `requests`, `announced`, `scan_state`, `user_activity`; storage DBs — `movie`, `tv`.

## Ingestion pipeline (deep dive)

**Trigger:** `plugins/receiver.py` handler on `filters.channel & (filters.document | filters.video)`.

**Flow:** skip-channel check → manual channel/session handling → subtitle routing (`is_subtitle_file`) → `_is_supported_media()` (rejects files without a resolution/quality) → `_extract_fields()` → `metadata(...)` provider resolution → `file_queue` → `process_file()` worker (serialized by `db_lock`) → `db.insert_media` → auto-catalog sync + `announce_new_media` + `auto_fulfill`.

**Filename rules:**
- Movies: `Title.Year.Quality...` — e.g. `Oppenheimer.2023.1080p.BluRay.x264.mkv`.
- TV: `Title.SxxExx.Quality...` (also `NxNN` / `"Season N"` anchors). Quality (resolution) is **required** — no resolution → skipped or routed to skip channel.
- Split files: `name.ext.001/.002…` joined into one stream via `group_key = f"{channel}:{quality}:{normalized_base}"`; `Part 01`/`CD01`/`Disc02` styles are deliberately **not** joined. Split zip archives (`.zip.001…`) join + seek-stream only if entries are **stored (uncompressed)**.
- Combined packs: `E01-E04`, `combined`, or season packs (`S03`) → stored as `season_number = 0` (`COMBINED_SEASON`), `episode_number = 1000 + season` (`COMBINED_EPISODE_BASE`), titled `"Season N Combined"`.
- Anime channels (`anime_channels`): Kitsu-first resolution, absolute-episode recovery (`One Piece - 1172.mkv`), `is_anime` flag, `episode_maps.py` absolute→S/E conversion.
- Manual channels (`manual_channels`): never auto-indexed; files attach to the active **Manual Upload Session** (`Backend.MANUAL_SESSION`, set via Tools page; `kind: "personal"|"real"`, fallback season). Personal titles use negative `tmdb_id`.
- Caption stamping: after indexing the bot edits the caption to append the IMDb/TMDb id (`caption_with_id`) so re-forwards and scans match instantly via `extract_default_id`.

**Edits/deletions:** `file_edited_handler` reindexes on caption override-id edits; `file_deleted_handler` purges DB entries for deleted messages. `replace_mode` deletes the old same-quality Telegram message on replacement; `duplicate_protection` skips exact duplicates (bot deletes the duplicate message).

**Scans:** `ScanManager` iterates channel history in batches of 200 (`SCAN_BATCH_SIZE`), persists progress in `tracking.scan_state` (`_id="scan"`), modes `scan`/`rescan`, resumable after restart. Controlled from the Tools page (`/api/admin/tools/...`).

## Configuration reference

### Env vars (`config.env`, first-boot seed only)

**Required:** `API_ID`, `API_HASH`, `BOT_TOKEN` (Telegram bot credentials), `OWNER_ID` (numeric), `DATABASE` (comma-separated Mongo URIs, **≥2** — index 0 = tracking DB, rest = storage DBs), `PORT` (default 8000).

**Optional fallbacks:** `REPLACE_MODE`, `HIDE_CATALOG`, `AUTH_CHANNEL` (CSV), `TMDB_API`, `TVDB_API`, `BASE_URL`, `UPSTREAM_REPO` / `UPSTREAM_BRANCH`, `ADMIN_USERNAME` / `ADMIN_PASSWORD` (default `admin`/`admin`), `SUBSCRIPTION`, `SUBSCRIPTION_GROUP_ID`, `APPROVER_IDS`, `HTTP_Proxy_URL` (legacy capitalization), `SHOW_ProxyAndNonProxyBoth`, `WEBDAV_USER` / `WEBDAV_PASSWORD`.

### DB-backed settings (`SettingsManager`, editable in web Settings page)

- Media: `replace_mode`, `duplicate_protection`, `hide_catalog`
- Channels: `auth_channels`, `anime_channels`, `manual_channels`, `channel_titles`, `skip_channel`, `delete_on_metadata_fail`, `announce_new_content`, `announcement_channel`, `global_search`, `global_search_channels`
- APIs: `tmdb_api`, `tvdb_api`, `base_url`
- Admin: `admin_username`, `admin_password`, `session_secret`
- Subscriptions: `subscription`, `subscription_group_id`, `approver_ids`, `payment_instructions`, `payment_qr_url`
- Proxy: `http_proxy_url`, `show_proxy_and_non_proxy_both`, `mediaflow_proxy`, `mediaflow_password`
- WebDAV: `webdav_user`, `webdav_password`
- Clients/DBs: `multi_tokens`, `extra_databases`
- Posters: `better_poster_enabled`/`better_poster`, `rpdb_enabled`/`rpdb_api_key`, `fanart_enabled`/`fanart_api_key`/`fanart_shuffle`/`fanart_shuffle_interval`/`fanart_low_res_poster`
- Upstream: `upstream_repo`, `upstream_branch`

**Validation rules:** channel fields must be `-100…` ids; a channel may belong to only one role (AUTH∩ANIME overlap allowed); only one poster provider at a time; fanart requires a key.

## Data model & collections

- **Tracking DB:** `settings` (`_id="app_settings"`), `state` (`db_index`, `catalog_order`, `auto_catalog_settings`, `user_session`), `users` (subscription_status, subscription_expiry, plan fields), `sub_plans`, `api_tokens`, `custom_catalogs`, `subtitles`, `requests`, `announced`, `scan_state`, `user_activity`.
- **Storage DBs:** `movie` (indexed on `tmdb_id`, `imdb_id`, `kitsu_id`; `telegram[]` qualities), `tv` (embeds `seasons[].episodes[].telegram[]`). Pydantic schemas in `modal.py`: `QualityPart`, `QualityDetail`, `Episode`, `Season`, `TVShowSchema`, `MovieSchema`.
- **Token doc shape:** `token` (32 chars), `user_id`, `is_admin`, `subscription_exempt`, `expires_at`, `limits {daily_limit_gb, monthly_limit_gb}`, `usage {total_bytes, daily, monthly}` (rolled by date-string compare), free-form `config` (quality sort, hidden catalogs, catalog order).
- **Id encoding** (`encrypt.py`): stream/subtitle/thumb ids = base62(zlib(json)) — compaction/obfuscation, **reversible**, not security. Telegram channel ids stored normalized positive; prefixed `-100` for API calls.

## URL surface

- **Addon:** `/stremio/{token}/manifest.json`, `/stremio/{token}/catalog/{movie|series}/{id}[/{extra}].json`, `/stremio/{token}/meta/{movie|series}/{id}.json`, `/stremio/{token}/subtitles/{type}/{id}.json`
- **Streams:** `GET|HEAD /dl/{token}/{id}/{name}`, `/sub/{token}/{id}/{name}`, `/thumb/{id}` (public), `/stream/stats`
- **WebDAV:** `/webdav/{token}/...` (read-only), `POST /webdav/{token}/refresh`
- **Admin API:** `/api/media/*`, `/api/tokens*`, `/api/admin/*` (settings, stats, health, logs, backup, restart, subscriptions, access, requests, stream-analytics, tools — scan/dbcheck/duplicates/dead-links/manual-session/bot-admin), `/api/custom-catalogs*`, `/api/system/*`
- **Pages:** `/` (dashboard), `/login`, `/logout`, `/admin/dashboard`, `/admin/settings`, `/admin/tools`, `/media/manage`, `/media/edit`, `/catalogs`, `/status` (public), `/stremio` (guide), `/request` (public), `/open/{stremio|nuvio}/{type}/{id}` (deep links), PWA: `/manifest.webmanifest`, `/sw.js`, `/pwa-icon.svg`

## Data flows

1. **Ingestion:** forward file → receiver handler → metadata resolution → queue → `insert_media` → announce + catalog sync + request auto-fulfill.
2. **Streaming:** Stremio fetches manifest/catalog/meta → stream id → `GET /dl/{token}/{id}/{name}` → `verify_token()` (expiry, subscription, GB limits) → decode id → `select_best_client()` (least workload + 3× failure penalty, DC preference, round-robin) → `ByteStreamer.prefetch_stream()` (1 MB chunks, prefetch, multi-client parallel) → 206 partial responses.
3. **Token auth:** `verify_token()` checks `api_tokens`, owner-linked `is_admin`, `expires_at`, subscription expiry, then daily/monthly GB (`limit_exceeded`).
4. **Settings hot-reload:** `SettingsManager.update()` validates → `_sync_channel_titles()` → reload extra DBs → save → `_reinit_dependent()` (restarts multi-token clients, userbot, global search flag).
5. **Restart/update:** web Restart → `uv run update.py` (destructive self-update) → `os.execl(uv, "run", "-m", "Backend")`.

## Run & deploy

```bash
uv run -m Backend          # entry point (Backend/__main__.py) — must run from repo root
uv run update.py           # self-update (destructive: resets .git, pulls upstream branch)
python bump-version.py [patch|minor|major]  # bumps pyproject.toml + Backend/__init__.py
```

- Web server = uvicorn started in-process on `0.0.0.0:PORT` (default 8000). Docker CMD: `bash start.sh` → `uv run update.py && uv run -m Backend`.
- `docker-compose.yaml`: mounts `./config.env:/app/config.env`, port `8000:8000`.
- `Caddyfile`: reverse proxy example (`reverse_proxy localhost:8000`) — gitignored.
- `config.env` contains real credentials — never commit (gitignored).
- Local dev: copy `sample_config.env` → `config.env`, `uv sync`, then `uv run -m Backend`.
- `update.py`: wipes `log.txt`, deletes `.git`, re-inits with hardcoded identity (`weebzone` / `doc.adhikari@gmail.com`), `git fetch origin && git reset --hard origin/{branch}`. Upstream resolution: DB `settings.upstream_repo/branch` > env > default `https://github.com/weebzone/Telegram-Stremio` / `master`. Runs on every `start.sh` boot and on web Restart.
- `bump-version.py`: `python bump-version.py [patch|minor|major]` (default patch) — reads the project name via `tomllib`, regex-replaces semver in `pyproject.toml`, `Backend/__init__.py`, and the local `[[package]]` entry (`source = { virtual = "." }`) in `uv.lock` (PEP 503-normalized name lookup).
- Heroku: `heroku.yml` → `build.docker.web = Dockerfile`.

## Conventions & gotchas

- Use `LOGGER` from `Backend.logger` for logging. Async-first (Motor/httpx/pyrogram). Blocking CPU work offloaded to `ThreadPoolExecutor` (see `encrypt.py`).
- `#-----` section separators, imperative docstrings, singleton globals (`db`, `file_queue`, `ACTIVE_STREAMS`).
- Must run from repo root — relative paths (`Backend/fastapi/templates`, `log.txt`, `.restartmsg`) depend on cwd.
- Stream ids are reversible (base62+zlib, not secure); Telegram channel ids normalized positive in DB, prefixed `-100` for API calls.
- Personal/manual titles use negative `tmdb_id`. Combined packs in season 0 sort as "Specials".
- `update.py` is destructive (`shutil.rmtree(".git")`, hard reset, hardcoded git identity) — careful when testing.
- Restart = re-exec `uv run -m Backend` after the updater.
- Fanart / BetterPoster / RPDB are mutually exclusive poster providers (validated in settings API).
- Minor lint noise: duplicated `import asyncio` at top of `api_routes.py`.
- Local divergence from upstream: the donation stream entry (`_donation()` in `stremio_routes.py`, prepended to every stream list and returned alone when nothing matched) is removed; empty results return `{"streams": []}`. Re-check after every upstream merge — it lives in the hot path of `get_streams`.

## Troubleshooting playbook

- **No streams / empty stream list:** check token `config.quality_filter` (falls back to all if it would hide everything), verify storage DB `movie`/`tv` docs, run dead-link check, check scan state.
- **Streams won't play:** verify token GB limits (`limits`, `usage`), `expires_at`/subscription expiry, MediaFlow/proxy URL config, client failures (`client_failures`).
- **Bot not indexing files:** bot must be admin in each `auth_channel`; file must include a resolution; check skip-channel routing (`delete_on_metadata_fail`); watch `log.txt`.
- **Global search unavailable:** requires a connected Userbot session (Settings → Session login; stored encoded in `tracking.state["user_session"]`); `global_search` is rejected when no userbot.
- **Wrong metadata:** use caption stamping/override id or the media edit page; check TMDB/TVDB keys; thresholds in `common.py`.
- **Server won't start:** run from repo root (relative paths), `DATABASE` needs ≥2 URIs, port free, `uv` installed.
- **Update/restart issues:** `update.py` is destructive (resets `.git`) — avoid on forks; restart re-execs `uv run -m Backend` after the updater.

## Common task procedures

**Add an admin API endpoint:** add a route in `Backend/fastapi/routes/api_routes.py` guarded with `Depends(require_auth)`; call DB methods on `Backend.db`; validate settings through `SettingsManager.update()` when needed.

**Add a Stremio addon feature:** edit `routes/stremio_routes.py` (manifest/catalog/meta); respect per-token visibility helpers (`_visibility_query`, `_token_can_view`) and `verify_token`.

**Change configuration:** add the property to `Settings` in `settings_manager.py`, seed from env in `_seed_from_env()`, validate in `update()`, then expose in the Settings page (`templates/settings.html` + settings API handler).

**Add a metadata provider:** implement search/detail/build-payload in `Backend/helper/metadata/providers/`, register in `resolvers.py` priority chains, mind `cached_call()` + semaphore in `common.py`.

**Debug streaming:** trace `stream_routes.stream_handler` → `custom_dl.ByteStreamer` (or `virtual_dl` / `zip_stream` / global variants); check client selection in `select_best_client()` and token limits in `security/tokens.py`.

---

# Exhaustive reference (appendix)

## Module internals

### `subtitles.py`
- Extensions: `SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")`; `is_subtitle_file()`, `subtitle_ext()`.
- `_LANGUAGES`: **48** entries as `(ISO 639-2/T code, label, aliases)` — e.g. `eng/English`, `hin/Hindi`, `tam/Tamil`, `bho/Bhojpuri` (no 639-1). Built at import: `_LANG_BY_TOKEN`, `_LANG_WORDS`.
- `detect_language()`: tokenizes lowercase, strips extension tokens and `_IGNORE_TOKENS = {"forced","sdh","cc","full","default","hearing","impaired","dubbed","dub","sub","subs","subtitle","subtitles"}`; scans tokens from the end (full names ≥4 chars → 3-letter codes → 2-letter codes); fallback `("und", "Unknown")`.
- `ingest_subtitle()`: identifies title (`extract_default_id` + `parse_media_name`; TV if season+episode), upserts into `tracking.subtitles` (`imdb_id, media_type, season, episode, lang_code, lang_label, name, chat_id, msg_id, encoded, source:"auto"`).
- `stremio_subtitle_entries()`: entries `{"id": "tg-{msg_id}", "url": "{base}/sub/{token}/{encoded}/subtitle{ext}", "lang": label}`; duplicate labels get numeric suffix.
- Manual: `list_languages()`, `resolve_subtitle_message()`, `manual_ingest_subtitle()`, `list_title_subtitles()`, `get_subtitles_for()`, `remove_subtitle()`.

### `requests_manager.py`
- `STATUSES = ("pending","uploaded","denied","banned")`; `_IMDB_RE = re.compile(r"(tt\d{7,10})")`; IP hashed via sha256 `[:16]`.
- Public: `search_titles`, `media_exists`, `submit_request`, `list_requests`, `popular_pending(limit=12)`, `set_status`, `delete_request`, `auto_fulfill`.
- `search_titles` chain: Cinemeta first, TMDB fallback (imdb → `_cinemeta_id_search`/`_tmdb_imdb_search`; name → `_cinemeta_name_search` limit 8 → `_tmdb_name_search`); dedupe by `(media_type, imdb_id or tmdb:{id})`, cap 15.
- `submit_request` reasons: `created|added|already_available|reopened|banned|invalid`. `auto_fulfill` marks matching pending → `uploaded`.

### `task_manager.py`
- `DELETE_BATCH_SIZE = 10`; `_FALLBACK_WORTHY = (ChatAdminRequired, ChannelPrivate, MessageDeleteForbidden, MessageAuthorRequired, UserNotParticipant, RPCError)`; `_SESSION_DEAD = (AuthKeyUnregistered, SessionRevoked)`.
- Functions: `edit_message`, `delete_message`, `delete_messages_batch`, `_userbot_edit`, `_delete_chunk`.
- Retry: `FloodWait` → sleep then retry once; `PeerIdInvalid` → `_resolve_peer` then retry once; other fallback-worthy errors → Userbot. Userbot disabled for the run after a dead-session error.

### `backup.py`
- Excludes `admin_password` and `session_secret`. Backs up settings + `custom_catalogs`, `sub_plans`, `api_tokens`.
- `_jsonify` (ObjectId→str, datetime→iso), `_revive` (24-char `_id`→ObjectId, ISO strings→datetime). `import_config` rejects payloads whose `app != "telegram-stremio"`; collections are **replaced** wholesale.

### `fanart.py`
- `fanart_artwork(imdb_id, tmdb_id, media_type) → {"poster","logo","background"}`. Endpoints: `/v3/movies/{id}`, `/v3/tv/{id}`, TMDB `/3/tv/{id}/external_ids`.
- Caches: `_CACHE_TTL = 6h`, `_ERROR_TTL = 300`, `_CACHE_MAX = 4096`, `_fetch_sem = Semaphore(10)`, in-flight dedupe dict.
- Shuffle: English-preferred; non-shuffle → max `likes`; shuffle with interval → time-bucket seeded `random` (`interval` minutes, default 5). Poster/logo use `/preview/`; backgrounds proxied via `wsrv.nl`.

### `analytics.py` / `health.py` / `pinger.py` / `passwords.py` / `encrypt.py`
- Analytics: `client_ip_from` (tries `cf-connecting-ip`, `x-real-ip`, `x-forwarded-for`, client host), `parse_app`/`parse_device` (UA maps), `lookup_ip` via `http://ip-api.com/json/{ip}`, `record_client`, `record_stream_start`, `get_activity_overview(page, per_page=5)`. `_IP_TTL = 6h`, `ONLINE_WINDOW = 120`.
- Health: checks `databases`, `bots`, `tmdb`, `base_url`; `_FREE_TIER_BYTES = 512 MB`; TTLs 30/300/60 s; status `critical|warning|ok`.
- Pinger: `sleep_time = 1200` s, GETs `{base_url}/status` (public status page) with `allow_redirects=True`, aiohttp timeout 15 s; skips with a warning when `base_url` is unset, warns on non-2xx/3xx responses.
- Passwords: `pbkdf2_sha256$200000$salt$digest` (16-byte salt), `hmac.compare_digest`, legacy plaintext fallback.
- Encrypt: `BASE62_ALPHABET` (digits, lowercase, uppercase), zlib `Z_BEST_COMPRESSION`, module-level `ThreadPoolExecutor`.

### `custom_dl.py` (ByteStreamer)
- `CHUNK_SIZE = 1 MB`, `CLEAN_INTERVAL = 30 min`, `TEST_CHUNK_SIZE = 100 MB`, `STALE_STREAM_IDLE = 180`.
- `ACTIVE_STREAMS` dict + `RECENT_STREAMS` deque(maxlen=20); stale cleaner every 30 s decrements `work_loads`.
- Prefetch queue maxsize = prefetch; parallel chunk fetches; fetch timeout 15 s; retries `<3` / flood `<5` (backoff `min(0.5*2**(tries-1), 10)`); FILE_REFERENCE errors refresh location; consumer stalls after 90 s.
- Registry entry (local `stream_entry` alias) fields: `start_ts`, `last_ts` (falls back to `start_ts`), `total_bytes`, `recent_measurements` deque(maxlen=3) via `setdefault`, `instant_mbps`/`avg_mbps`/`peak_mbps`, `status`, `chunk_size`; finished entry logged via `db.log_stream_stats(stream_entry)` and popped into `RECENT_STREAMS` after a 3 s delay.
- `_prewarm_sessions()`: DCs `[1, 2, 4, 5]`, `no_updates`, up to 6 authorization retries. Speed test: ping `limit=4096`, chunk `512 KB`, `max_concurrent_chunks = 8`.

### `bot.py` / `clients.py`
- `StreamBot`: `sleep_threshold=20, workers=6, max_concurrent_transmissions=10`, plugins root `Backend/pyrofork/plugins`. `USERBOT_CLIENT_INDEX = -1`; userbot `no_updates=True, in_memory=True`.
- Registries: `multi_clients`, `work_loads`, `client_dc_map`, `client_failures`, `client_avg_mbps`.
- `TokenParser.parse_from_settings()`: 1-based ids from `multi_tokens` (0 = main bot); `reload_multi_token_clients()` diffs and hot-reloads.

### `global_search.py`
- Limits: `MAX_RESULTS = 50`, `SEARCH_COOLDOWN_SECONDS = 5`, `MAX_CONCURRENT_SEARCHES = 3`, `MAX_CONCURRENT_CHANNELS = 5`, `MIN_TITLE_SCORE = 0.7`, `RESULT_CACHE_SECONDS = 60`, `SPLIT_SCAN_WINDOW = 20`.
- Query variants: `"Title S02E03"`, `"Title 101"` (absolute), `"Title {year}"`, bare title, `Title E101`, symbol-stripped. Split parts gathered within ±20 messages of the seed.

### `scan_manager.py`
- `SCAN_BATCH_SIZE = 200`, `SCAN_MAX_EMPTY_BATCHES = 10`, `SCAN_MAX_ID_CAP = 1_000_000`, `SCAN_BATCH_DELAY = 0.5`, `SCAN_PROCESS_CONCURRENCY = 8`. DB check: concurrency 5, page size 100.
- `tracking.scan_state` `_id="scan"` fields: `status, mode, selected_channels, pending, current_channel, current_id, cursors, counters{...}, started_at, updated_at, finished_at, error`.

### `metadata/common.py` thresholds
- `CINEMETA_THRESHOLD = 0.60`, `TMDB/TVDB/KITSU_THRESHOLD = 0.55`, `STRONG_MATCH = 0.92`, `ALT_TITLE_LOOKUPS = 5`, `API_SEMAPHORE = Semaphore(12)`, `COMBINED_SEASON = 0`, `COMBINED_EPISODE_BASE = 1000`.

### `auto_catalog.py`
- `AUTO_CATALOG_REGION = "IN"`, sync concurrency 5 (instant 3). `AUTO_CATALOG_DEFINITIONS` (26): language (bollywood, hollywood, anime, kdrama, bengali, south_indian, tamil, telugu, malayalam, kannada, japanese, korean), smart (top_rated, recently_added), OTT (netflix, prime_video, hotstar, apple_tv, hulu, hbo, jiocinema, zee5, sonyliv, mx_player, crunchyroll — 12).

### `webdav_fs.py`
- `VNode`: `path, name, is_dir, size, mtime, content_type, kind` (`movie_video|movie_nfo|show_nfo|season_nfo|episode_video|episode_nfo|poster`), `stream_id, stream_name, parts, nfo_body, media_type, tmdb_id, db_index, season_number, episode_number, children`.
- Tree: `/Movies/Title (Year)/…` + `/TV Shows/Show (Year)/Season 01/Show S01E01 - Episode Title - 1080p.mkv`; every quality = separate file.
- `pick_best_quality`: order `2160p/4k=0, 1440p=1, 1080p=2, 720p=3, 480p=4, 360p=5`, unknown 50. Singleton `fs` with `cache_ttl=300`.

### `virtual_dl.py` / `zip_stream.py`
- `resolve_virtual_parts()` computes per-part `file_id`, sizes, and `cum_start` cumulative offsets. `virtual_stream_generator()` walks overlapping parts, computes local offsets, chains `prefetch_stream` per part with `stream_id = "{id}-p{index}"`, aborts on client disconnect.
- `zip_stream`: `STORED = 0` only streamable; `parse_local_header()` (`PK\x03\x04`, `data_offset = 30 + name_len + extra_len`, `has_descriptor = flag & 0x08`); `_parse_central_directory()` Zip64-aware (EOCD `PK\x05\x06`, locator `PK\x06\x07`, record `PK\x01\x02`); `resolve_zip_entry()` reads local head (64 KB) then falls back to central directory; serves inner-file byte ranges via concatenated archive offsets.

## Stremio routes internals

- Manifest: `ADDON_NAME = "Telegram"`, `id: telegram.media.{token[:8]}`, `idPrefixes: ["tt","tg","kitsu"]`, `behaviorHints: {configurable: true, configurationRequired: false}`, `types: ["movie","series"]`, resources include `catalog` unless `hide_catalog`; one config entry (manifest_url). `PAGE_SIZE = 15`, 20 `GENRES`.
- Built-in catalogs: `latest_movies`, `top_movies` (with search), `latest_series`, `top_series`; custom = `custom_{_id}`.
- `addon_version` = version or `f"{ADDON_VERSION}-{epoch_tag}"` where `epoch_tag = format(int(expiry.timestamp()) & 0xFFFF, "x")`; expiry note in description.
- Visibility: per-item `public|tokens|owner` + `allowed_tokens`; `_token_can_view` (owner always; `tokens` needs membership; subscription mode needs not-expired); `_not_exclusive_clause` hides `exclusive_catalog_id` docs unless `exclusive_searchable`.
- Membership cache: `_MEMBERSHIP_TTL = 60` s, max 5000 entries, fail-open.
- Kitsu ids: `kitsu:<id>:<season>:<episode>` or `kitsu:<id>:<abs_ep>`; `_parse_stremio_id` splits on `:`.
- Pseudo-streams: subscription expired → "🚫 Plan Expired"; group gate → "📢 Join Required"; `limit_exceeded` → "Limit Reached" with `url = tg://user?id={OWNER_ID}`.
- Sorting: combined → `episode_start`, `name_key`, then `get_resolution_priority` (reverse unless `quality_sort == "asc"`); else `(resolution_priority, size_bytes)`.
- `get_resolution_priority`: `{2160p/4k/uhd:2160, 1080p/fhd:1080, 720p/hd:720, 480p/sd:480, 360p:360}`, default 1. `stream_res_label`: `{2160:"4K", 1080:"1080p", 720:"720p", 480:"480p", 360:"360p"}`, default `"other"`.
- Poster providers: BetterPoster `https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg`; RPDB free tier or keyed `https://api.ratingposterdb.com/{key}/imdb/poster-default/{imdb_id}.jpg`.
- `build_proxy_url`: plain = `base + url`; MediaFlow = `{base}/proxy/stream?d={quote(url)}&api_password=...`.
- `_global_streams_for`: Kitsu/Cinemeta title resolution, SxxExx→absolute mapping, `global_search(...)`, absolute-first results.
- Also: `GET /{token}/configure` (HTML), `GET|POST /{token}/addon-config` (quality filter values `{"480p","720p","1080p","4K"}`).

## Stream routes internals

- Dispatch order on decoded id: `global+zip → global_zip_media_streamer`; `global+parts → global_virtual_media_streamer`; `global → global_media_streamer`; `parts+zip → db_zip_media_streamer`; `parts → virtual_media_streamer`; else `media_streamer` (`chat_id = int(f"-100{decoded['chat_id']}")`).
- `parse_range_header`: handles `bytes=-N` and `bytes=N-`; 416 + `Content-Range: bytes */{size}` on bad ranges.
- `select_best_client(target_dc)`: score `work_loads + 3 * client_failures`; DC-preferring then round-robin tie-break. `decay_client_failures()`: every 300 s, -1 per client.
- `get_parallel_prefetch(client_count)` = `min(max(ceil(count/5), 1), 5)`.
- Headers: 206 + Content-Range; `Accept-Ranges: bytes`, `Cache-Control: public, max-age=3600`, CORS `*`, `Content-Disposition` with UTF-8 fallback.
- Stream `meta` includes `file_name` (from `_resolve_filename_mime`, computed before building headers) and `title` falls back to `file_name` when `_lookup_title` returns nothing.
- `/thumb/{id}` TTL 3600; `/stream/stats` returns `{active, recent}` with mbps/duration fields plus `file_name` (title falls back to `file_name`); `_SUBTITLE_MIME` covers all 5 subtitle exts.

## Full `/api/*` route table

**Media** — `GET /api/media/list` · `DELETE /api/media/delete` · `PUT /api/media/update` · `DELETE /api/media/delete-quality|delete-tv-quality|delete-tv-episode|delete-tv-season` · `POST /api/media/resolve-telegram` · `POST /api/media/manual-add` · `GET /api/media/manual-add/catalogs|resolve-meta` · `GET /api/media/rescan/search` · `POST /api/media/rescan/apply`

**Subtitles** — `GET /api/media/subtitles/languages` · `GET /api/media/subtitles` · `POST /api/media/subtitles/resolve|add|remove`

**Tokens/system** — `POST /api/tokens` · `PUT|DELETE /api/tokens/{token}` · `GET /api/system/workloads|stats|speedtest` · `GET /api/system/speedtest/stream` (SSE: `start/progress/result/done`)

**Access** — `GET /api/admin/access/tokens` · `DELETE /api/admin/access/tokens/{token}` · `POST /api/admin/access/users/{user_id}/assign-plan` · `PATCH /api/admin/access/tokens/{token}/link-user|lifetime` · `POST /api/admin/access/tokens/{token}/expiry` · `POST /api/admin/access/grant-lifetime` · `GET /api/admin/subscriptions/preflight` · `POST /api/admin/subscriptions/backfill-names`

**Subscriptions** — `GET|POST /api/admin/subscriptions/plans` · `PUT|DELETE /api/admin/subscriptions/plans/{plan_id}` · `GET /api/admin/subscriptions/users` · `POST /api/admin/subscriptions/users/{user_id}/manage`

**Requests** — `GET /api/request/search|popular` · `POST /api/request/submit` · `GET /api/admin/requests` · `PATCH|DELETE /api/admin/requests/{request_id}`

**Custom catalogs** — `GET|POST /api/custom-catalogs` · `PUT|DELETE /api/custom-catalogs/{catalog_id}` · `POST|GET /api/custom-catalogs/media-visibility` · `GET /api/custom-catalogs/search-media` · `POST /api/custom-catalogs/auto-sync` · `GET /api/custom-catalogs/auto-sync/status` · `GET|PUT /api/custom-catalogs-order` · `GET|PUT /api/custom-catalogs/auto-sync/settings` · `GET|POST|DELETE /api/custom-catalogs/{catalog_id}/items`

**Settings/session** — `GET|PUT /api/admin/settings` · `GET /api/admin/settings/session` · `POST .../send-code|verify-code|verify-password|disconnect|reconnect` · `DELETE /api/admin/settings/session`

**Admin/system** — `GET /api/admin/system-stats` · `POST /api/admin/clear-cache` · `GET /api/admin/dead-links` · `GET /api/admin/stream-analytics|user-activity` · `POST /api/admin/clear-analytics` · `GET /api/admin/stats|health|health/report|setup-status` · `GET /api/admin/backup/export` · `POST /api/admin/backup/import` · `GET /api/admin/logs|logs/download` · `POST /api/admin/restart`

**Tools** — `GET /api/admin/tools/channels` · `GET /api/admin/tools/bot-admin/scan` · `POST /api/admin/tools/bot-admin/apply` · `GET /api/admin/tools/bot-admin/apply/status` · `GET|POST|DELETE /api/admin/tools/manual-session` · `GET /api/admin/tools/manual-session/search` · `POST /api/admin/tools/scan/start|cancel` · `GET /api/admin/tools/scan/status` · `POST /api/admin/tools/dbcheck/start|cancel` · `GET /api/admin/tools/dbcheck/status` · `POST /api/admin/tools/dead-links/purge` · `POST /api/admin/tools/duplicates/start|cancel|purge` · `GET /api/admin/tools/duplicates/status`

Note: `/api/media/details` is defined but **not registered** in `main.py`.

`restart_app_api()`: sleep 1 s → run `uv run update.py` (waited) → `os.execl(uv, "run", "-m", "Backend")`.

## Themes & styles

- **Themes (12)**: `graphite_amber`, `amoled_midnight`, `obsidian_emerald`, `royal_violet`, `slate_ocean`, `charcoal_violet`, `fresh_canopy`, `tiffany_noir`, `rose_quartz` (light), `daylight_sky` (light), `sage_linen` (light), `golden_hour` (light). Each: `name, is_dark, colors{...}, css_classes`.
- **Styles (3)**: `default`, `glassy`, `neo_brutal`. Defaults: `graphite_amber` / `default`.

## Templates (14)

`access_manage.html` (tokens & access), `admin_dashboard.html`, `base.html` (layout), `custom_catalogs.html`, `dashboard.html`, `login.html`, `media_edit.html`, `media_management.html`, `request_public.html`, `requests_manage.html`, `settings.html`, `stremio_configure.html` (addon install), `subscriptions_manage.html`, `tools.html`.

## Bot flows

- **`/start` (free mode):** only `OWNER_ID` gets a personal token + manifest link; others ignored.
- **`/start` (subscription mode):** active user → `ensure_api_token_for_user`; expired → marked; otherwise plan keyboard `callback_data=f"plan_{id}"` ("`<days> Days - <sym><price>`").
- **Purchase:** `plan_selection` (expiry extends if active, `set_pending_payment`, DMs instructions with `ForceReply`, optional QR photo) → `handle_payment_screenshot` (forwards to approvers with `approve_{uid}`/`reject_{uid}`) → `admin_review` (approve: token + invite link for group; reject: notice; captions updated "✅ Approved by …"/"❌ Rejected by …"). `/status` reports remaining time.
- **Group security:** on `chat_member_updated` in `subscription_group_id` → ban+unban kick for inactive non-owner/non-approver joiners + DM.
- **`clients.py`:** `multi_clients[0] = StreamBot`; extra tokens 1-based; extras created with `sleep_threshold=100, no_updates=True, in_memory=True`.

## `Settings` class — all 43 properties

- **Booleans (14):** `replace_mode`, `duplicate_protection`, `hide_catalog`, `subscription`, `show_proxy_and_non_proxy_both`, `mediaflow_proxy`, `global_search`, `announce_new_content`, `delete_on_metadata_fail`, `better_poster_enabled`, `rpdb_enabled`, `fanart_enabled`, `fanart_shuffle`, `fanart_low_res_poster`
- **Strings (19):** `announcement_channel`, `skip_channel`, `tmdb_api`, `tvdb_api`, `base_url`, `upstream_repo`, `upstream_branch`, `admin_username`, `admin_password`, `session_secret`, `http_proxy_url`, `mediaflow_password`, `webdav_user`, `webdav_password`, `payment_instructions`, `payment_qr_url`, `better_poster`, `rpdb_api_key`, `fanart_api_key`
- **Lists (8):** `global_search_channels`, `anime_channels`, `manual_channels`, `channel_titles`, `auth_channels`, `approver_ids`, `multi_tokens`, `extra_databases`
- **Integers (2):** `subscription_group_id`, `fanart_shuffle_interval`

`SettingsManager`: `initialize`, `reload`, `current`, `update`, `_reinit_dependent`. Validation: channel ids must match `-100` + digits (len ≥ 8); channel appears in only one role (only AUTH∩ANIME overlap allowed); only one poster provider; `better_poster` must contain `{imdb_id}`; `fanart_enabled` requires key; `extra_databases` must start with `mongodb://` or `mongodb+srv://`.

## Database method groups

- Connection/storage: `connect`, `disconnect`, `update_current_db_index`, `connect_storage_db`, `disconnect_storage_db`, `get_database_list`, `reload_extra_databases`, `ensure_indexes`
- Settings: `get_settings`, `save_settings`, `get_catalog_order`, `save_catalog_order`
- Users/subscriptions: `get_user`, `is_subscription_active`, `set_pending_payment`, `approve_payment`, `reject_payment`, `get_expired_users`, `mark_user_expired`, `get_expiring_users`, `mark_reminder_sent`, `get_subscription_plans`, `add_subscription_plan`, `update_subscription_plan`, `delete_subscription_plan`, `get_all_subscribers`, `manage_subscriber`, `assign_subscription`, `set_user_never_expires`
- Catalogs: `create_custom_catalog`, `get_custom_catalogs`, `get_custom_catalog`, `update_custom_catalog`, `set_catalog_item_visibility`, `set_media_visibility`, `get_media_visibility`, `delete_custom_catalog`, `add_item_to_custom_catalog`, `remove_item_from_custom_catalog`, `find_media_doc`, `purge_media_from_catalogs`, `mark_item_exclusive`, `clear_item_exclusive`
- Media/streams: `get_media_ids_by_part`, `remove_media_part`, `insert_media`, `update_movie`, `update_tv_show`, `sort_movies`, `sort_tv_shows`, `search_documents`, `get_media_details`, `get_document(s)`, `update_document`, `delete_document`, `get_title_by_stream_id`, `delete_media_by_stream_id`, `delete_movie_quality`, `delete_tv_quality`, `delete_tv_episode`, `delete_tv_season`, `get_database_stats`, `replace_media_metadata`
- Tokens: `add_api_token`, `ensure_api_token_for_user`, `align_token_with_subscription`, `set_token_lifetime`, `update_token_expiry`, `grant_lifetime_to_unlinked`, `get_api_token`, `get_api_token_by_user`, `get_all_api_tokens`, `revoke_api_token`, `set_token_config`, `link_token_user`, `update_token_usage`, `update_api_token_limits`
- Dead links/analytics: `flag_dead_link`, `get_all_dead_links`, `log_stream_stats`, `get_stream_analytics`

Indexes: `custom_catalogs` (updated_at desc; items.tmdb_id+media_type); subtitles (chat_id+msg_id **unique**; imdb_id+season+episode; legacy `stream_id` indexes dropped); storage `movie`/`tv`: `tmdb_id`, `imdb_id`, `kitsu_id` asc. Tracking DB also holds `stream_analytics`.

Stream analytics: `log_stream_stats` stamps `logged_at` with `datetime.now(timezone.utc)` and updates the token's `last_active`/`last_title`/`user_name`; `get_stream_analytics` normalizes naive timestamps to UTC and returns `logged_at` as `%Y-%m-%dT%H:%M:%S.%f`[:-3] + `"Z"`.

## `verify_token()` order (security/tokens.py)

1. Missing token → `HTTPException(401)`.
2. `limit_exceeded = None`, `limit_video = None`, `subscription_expired = False`.
3. `is_admin` = flag OR `user_id == Telegram.OWNER_ID`.
4. Expiry (skipped for admins/exempt): token `expires_at` always enforced; then subscription mode requires active user, else `subscription_expired = True`.
5. Daily limit → `limit_exceeded="daily"`, video `https://bit.ly/3YZFKT5`.
6. Monthly limit → `limit_exceeded="monthly"`, video `https://bit.ly/4rfjtgd`.
Returns enriched `token_data` dict (early return, not exception) for expiry/limit cases.
