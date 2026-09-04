# Telegram-Stremio — Copilot Instructions

Always-on guidelines for working in this repository (self-hosted Telegram → Stremio media server, v5.x).

## Before you start

- Read `.github/skills/telegram-stremio/SKILL.md` before changing backend code. It is the authoritative project reference: architecture, module map, boot order, settings, route tables, constants, and troubleshooting.
- Explore the file(s) you are about to change and their callers before editing. This codebase uses cross-module singletons (`Backend.db`, `SettingsManager.current()`, `botmod.Userbot`, `multi_clients`) — a "local" change can break startup or streaming elsewhere.
- This repo has **no test suite**. Verify Python changes at minimum with syntax/import checks and by tracing callers; do not claim runtime correctness without evidence.

## Architecture essentials

- Single asyncio process: the PyroFork bot and the in-process uvicorn server share one event loop (`Backend/__main__.py`).
- Entry point: `uv run -m Backend` from the **repo root** (relative paths like `log.txt`, `.restartmsg`, `Backend/fastapi/templates` depend on cwd).
- Config: `config.env` is a **first-boot seed only**. Runtime settings live in MongoDB via `SettingsManager.current()`; hard env-only keys: `API_ID`, `API_HASH`, `BOT_TOKEN`, `DATABASE` (≥2 URIs), `PORT`, `OWNER_ID`.
- Data flow: Telegram channel files → `Backend/pyrofork/plugins/receiver.py` → `metadata(...)` resolution → `Backend.db.insert_media` → served via `Backend/fastapi/routes/*`.

## Commands

```bash
uv run -m Backend                          # run the server (repo root)
uv sync                                    # install deps after lockfile changes
python bump-version.py [patch|minor|major] # bump pyproject.toml + Backend/__init__.py
```

- Do **not** run `uv run update.py` — it deletes `.git` and hard-resets to upstream. It runs automatically at deploy time.
- Docker: `docker compose up` (CMD is `bash start.sh`); Heroku via `heroku.yml`.
- Check for problems with `uv run python -m py_compile <changed files>` or import checks; do not start a full server unless asked.

## Conventions

- Logging: `from Backend.logger import LOGGER` only. Never `print()` for runtime output.
- Async-first: Motor for Mongo, httpx for HTTP, async pyrogram APIs. Offload blocking CPU work to `ThreadPoolExecutor` (see `Backend/helper/encrypt.py`).
- Comments: `#-----` section separators; imperative docstrings; keep helper singletons (`db`, `file_queue`, `ACTIVE_STREAMS`) intact.
- IDs: stream/subtitle ids = base62(zlib(json)) via `encode_string`/`decode_string` — reversible, not security. Telegram channel ids are normalized positive in DB; prefix `-100` for API calls.
- Stremio routes must respect per-token visibility (`_visibility_query`, `_token_can_view`) and `verify_token` (`Backend/fastapi/security/tokens.py`).
- Admin API endpoints must be guarded with `Depends(require_auth)` and registered in `Backend/fastapi/main.py` (note: `/api/media/details` exists unregistered — do not copy that pattern).

## Adding/editing features (checklist)

1. Routes: `api_routes.py` (admin), `stremio_routes.py` (addon), `stream_routes.py` (streaming), `template_routes.py` (pages), `webdav_routes.py`.
2. Settings: add property to `Settings` + `_seed_from_env` + validation in `update()` (`settings_manager.py`), then expose via Settings page/API.
3. DB: collections in `Database` (`database.py`); schema docs in `Backend/helper/modal.py`; add indexes in `ensure_indexes()`.
4. Metadata: implement in `Backend/helper/metadata/providers/`, register in `resolvers.py` priority chains, respect `cached_call()` + `API_SEMAPHORE` in `common.py`.

## Safety rules

- Never commit, print, or share `config.env` contents, `Caddyfile`, session strings, or DB URIs. `config.env` and `Caddyfile` are gitignored.
- Do not modify `.github/skills/telegram-stremio/SKILL.md` casually — it mirrors the codebase; update it alongside material code changes so it stays accurate.
- This project contains no tests and no CI; make edits minimal, trace callers, and verify with syntax/import checks before reporting success.
