from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime
from time import time

from fastapi import HTTPException
from fastapi.responses import FileResponse

from Backend import StartTime, __version__, db
from Backend.helper.system.backup import export_config, import_config
from Backend.helper.system.health import run_health_checks
from Backend.helper.telegram.pyro import get_readable_time
from Backend.helper.settings.session import (
    disconnect_session,
    get_session_status,
    reconnect_session,
    remove_session,
    start_login,
    submit_code,
    submit_password,
)
from Backend.helper.settings.manager import SettingsManager
from Backend.logger import LOGGER
import Backend.pyrofork.bot as botmod
from Backend.pyrofork.bot import StreamBot

async def session_send_code_api(payload: dict):
    try:
        return await start_login(payload.get("phone"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def session_verify_code_api(payload: dict):
    try:
        return await submit_code(payload.get("login_id"), payload.get("code"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def session_verify_password_api(payload: dict):
    try:
        return await submit_password(payload.get("login_id"), payload.get("password"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def session_status_api():
    return await get_session_status()


async def session_disconnect_api():
    return await disconnect_session()


async def session_reconnect_api():
    try:
        return await reconnect_session()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def session_remove_api():
    return await remove_session()




# ─────────────────────────────────────────────────────────────────────────────
# Settings API
# ─────────────────────────────────────────────────────────────────────────────

async def get_settings_api() -> dict:

    data = SettingsManager.current().to_dict()
    data["admin_password_set"] = bool(data.get("admin_password"))
    data["admin_password"] = ""
    data["session_secret_set"] = bool(data.get("session_secret"))
    data["session_secret"] = ""

    try:
        data["database_list"] = db.get_database_list()
    except Exception as e:
        LOGGER.error(f"get_settings_api: could not load database list: {e}")
        data["database_list"] = []

    active = SettingsManager._all_channel_ids(data)
    titles = data.get("channel_titles") or {}
    if not isinstance(titles, dict):
        titles = {}
    titles = {str(k): str(v) for k, v in titles.items() if str(k) in active and v}
    missing = [cid for cid in active if cid not in titles]
    if missing:
        full = SettingsManager.current().to_dict()
        await SettingsManager._sync_channel_titles(full)
        try:
            await db.save_settings(full)
            SettingsManager._current = SettingsManager.current().__class__(full)
        except Exception as e:
            LOGGER.warning(f"get_settings_api: could not persist channel titles: {e}")
        titles = full.get("channel_titles") or {}
    data["channel_titles"] = {str(k): str(v) for k, v in (titles or {}).items() if k and v}

    return {"settings": data}


async def update_settings_api(payload: dict) -> dict:

    #----- Empty password string means leave it unchanged
    if "admin_password" in payload and not str(payload["admin_password"]).strip():
        del payload["admin_password"]
    if "session_secret" in payload and not str(payload["session_secret"]).strip():
        del payload["session_secret"]

    #----- Type coercion and validation
    bool_keys = {"replace_mode", "duplicate_protection", "hide_catalog", "subscription", "show_proxy_and_non_proxy_both", "mediaflow_proxy", "announce_new_content", "delete_on_metadata_fail", "better_poster_enabled", "rpdb_enabled", "fanart_enabled", "fanart_shuffle", "fanart_low_res_poster"}
    for key in bool_keys:
        if key in payload:
            payload[key] = bool(payload[key])

    list_str_keys = {"auth_channels", "multi_tokens", "extra_databases", "global_search_channels", "anime_channels", "manual_channels"}
    for key in list_str_keys:
        if key in payload:
            if not isinstance(payload[key], list):
                raise HTTPException(status_code=400, detail=f"'{key}' must be a list.")
            payload[key] = [str(v).strip() for v in payload[key] if str(v).strip()]

    if "better_poster" in payload:
        payload["better_poster"] = str(payload["better_poster"] or "").strip()
        if payload["better_poster"] and "{imdb_id}" not in payload["better_poster"]:
            raise HTTPException(status_code=400, detail="wrong betterposter url")

    if "rpdb_api_key" in payload:
        payload["rpdb_api_key"] = str(payload["rpdb_api_key"] or "").strip()

    if "fanart_api_key" in payload:
        payload["fanart_api_key"] = str(payload["fanart_api_key"] or "").strip()

    if "fanart_shuffle_interval" in payload:
        try:
            payload["fanart_shuffle_interval"] = max(0, int(payload["fanart_shuffle_interval"]))
        except (ValueError, TypeError):
            payload["fanart_shuffle_interval"] = 5

    if len([k for k in ("better_poster_enabled", "rpdb_enabled", "fanart_enabled") if payload.get(k)]) > 1:
        raise HTTPException(status_code=400, detail="Enable only one poster provider at a time")

    if payload.get("fanart_enabled") and not str(payload.get("fanart_api_key") or "").strip():
        raise HTTPException(status_code=400, detail="Fanart.tv API key is required")

    if "extra_databases" in payload:
        for uri in payload["extra_databases"]:
            if not uri.startswith(("mongodb://", "mongodb+srv://")):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid database URI (must start with mongodb:// or mongodb+srv://): {uri[:30]}…"
                )

    if "approver_ids" in payload:
        if not isinstance(payload["approver_ids"], list):
            raise HTTPException(status_code=400, detail="'approver_ids' must be a list.")
        try:
            payload["approver_ids"] = [int(v) for v in payload["approver_ids"] if str(v).strip()]
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="All approver_ids must be integers.")

    if "subscription_group_id" in payload:
        try:
            payload["subscription_group_id"] = int(payload["subscription_group_id"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="'subscription_group_id' must be an integer.")
    def _validate_channel_id(channel: str, field: str) -> str:
        channel = str(channel).strip()
        if not channel:
            return ""
        if not channel.startswith("-100") or not channel[4:].isdigit() or len(channel) < 8:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field}: '{channel}'. Only channel IDs in -100xxxxxxxxxx format are accepted (channels only, no groups/users/bots)."
            )
        return channel

    if "auth_channels" in payload:
        cleaned = []
        for channel in payload["auth_channels"]:
            c = _validate_channel_id(channel, "auth channel")
            if c:
                cleaned.append(c)
        payload["auth_channels"] = cleaned

    if "global_search_channels" in payload:
        cleaned = []
        for channel in payload["global_search_channels"]:
            c = _validate_channel_id(channel, "global search channel")
            if c:
                cleaned.append(c)
        payload["global_search_channels"] = cleaned

    if "anime_channels" in payload:
        cleaned = []
        for channel in payload["anime_channels"]:
            c = _validate_channel_id(channel, "anime channel")
            if c:
                cleaned.append(c)
        payload["anime_channels"] = cleaned

    if "manual_channels" in payload:
        cleaned = []
        for channel in payload["manual_channels"]:
            c = _validate_channel_id(channel, "manual channel")
            if c:
                cleaned.append(c)
        payload["manual_channels"] = cleaned

    if "announcement_channel" in payload and payload["announcement_channel"]:
        payload["announcement_channel"] = _validate_channel_id(
            payload["announcement_channel"], "announcement channel"
        )

    if "skip_channel" in payload and payload["skip_channel"]:
        payload["skip_channel"] = _validate_channel_id(
            payload["skip_channel"], "skip channel"
        )

    #----- The same channel id may not appear in more than one channel field.
    #----- Only AUTH ∩ ANIME is allowed, because an anime channel is an auth channel
    #----- that's flagged as anime (the receiver only indexes files from auth channels).
    _channel_fields = ("auth_channels", "manual_channels", "global_search_channels",
                       "anime_channels", "announcement_channel", "skip_channel")
    if any(field in payload for field in _channel_fields):
        current = SettingsManager.current()

        def _norm_ids(values) -> set:
            if isinstance(values, str):
                values = [values]
            return {str(c).strip().replace("-100", "") for c in (values or []) if str(c).strip()}

        groups = {
            "AUTH": _norm_ids(payload.get("auth_channels", list(current.auth_channels))),
            "MANUAL": _norm_ids(payload.get("manual_channels", list(current.manual_channels))),
            "GLOBAL SEARCH": _norm_ids(payload.get("global_search_channels", list(current.global_search_channels))),
            "ANIME": _norm_ids(payload.get("anime_channels", list(current.anime_channels))),
            "ANNOUNCEMENT": _norm_ids(payload.get("announcement_channel", current.announcement_channel)),
            "SKIP": _norm_ids(payload.get("skip_channel", current.skip_channel)),
        }

        allowed_overlap = frozenset({"AUTH", "ANIME"})
        names = list(groups)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                if frozenset({a, b}) == allowed_overlap:
                    continue
                clash = groups[a] & groups[b]
                if clash:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Channel {', '.join(sorted(clash))} can't be in both {a} and {b} channels — each channel may only belong to one field."
                    )

    #----- Strip whitespace from string fields
    for key in ("tmdb_api", "base_url", "upstream_repo", "upstream_branch",
                "admin_username", "admin_password", "session_secret", "http_proxy_url",
                "mediaflow_password", "payment_instructions", "payment_qr_url",
                "announcement_channel", "skip_channel"):
        if key in payload and isinstance(payload[key], str):
            payload[key] = payload[key].strip()

    if payload.get("admin_password"):
        payload["admin_password"] = hash_password(payload["admin_password"])

    try:
        reinit_results = await SettingsManager.update(db, payload)
        return {
            "message": "Settings saved successfully.",
            "reinit": reinit_results,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tools — WebUI replacement for /scan, /rescan, /scanstatus, /cancelscan, /dbcheck
# ─────────────────────────────────────────────────────────────────────────────

#----- Pick a Telegram client capable of fetching channel messages
    return None



#----- ── System & Maintenance (web replacements for /stats, /log, /restart) ──

LOG_FILE = "log.txt"


#----- Aggregate content + system metrics across all storage DBs (was /stats)
async def get_db_stats_api() -> dict:
    try:
        total_movies = total_tv = total_episodes = total_streams = total_db_size = 0

        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")
            if storage is None:
                continue

            total_movies += await storage["movie"].count_documents({})
            async for movie in storage["movie"].find({}, {"telegram": 1}):
                total_streams += len(movie.get("telegram", []))

            total_tv += await storage["tv"].count_documents({})
            async for show in storage["tv"].find({}, {"seasons": 1}):
                for season in show.get("seasons", []):
                    for episode in season.get("episodes", []):
                        total_episodes += 1
                        total_streams += len(episode.get("telegram", []))

            try:
                total_db_size += (await storage.command("dbStats")).get("dataSize", 0)
            except Exception:
                pass

        return {
            "status": "success",
            "data": {
                "version": __version__,
                "movies": total_movies,
                "tv_shows": total_tv,
                "episodes": total_episodes,
                "streams": total_streams,
                "uptime": get_readable_time(int(time() - StartTime)),
                "db_size": get_readable_file_size(total_db_size),
                "storage_dbs": db.current_db_index,
                "auth_channels": len(SettingsManager.current().auth_channels),
            },
        }
    except Exception as e:
        LOGGER.error(f"[Stats] Error: {e}")
        return {"status": "error", "message": str(e)}


#----- First-run setup checklist: what's configured vs still missing
async def export_config_api() -> dict:
    return await export_config()


#----- Config backup restore
async def import_config_api(payload: dict) -> dict:
    try:
        result = await import_config(payload)
        return {"status": "success", "result": result, "message": "Backup restored successfully."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOGGER.error(f"Config import error: {e}")
        return {"status": "error", "message": str(e)}


#----- Lightweight liveness probe; start_time changes on every boot (restart detection)
async def health_api() -> dict:
    return {"status": "ok", "start_time": StartTime, "version": __version__}


#----- Full diagnostics report (DBs, bot clients, TMDB, base URL)
async def health_report_api(force: bool = False) -> dict:
    try:
        return {"status": "success", "data": await run_health_checks(force=force)}
    except Exception as e:
        LOGGER.error(f"Health report error: {e}")
        return {"status": "error", "message": str(e)}


#----- Tail of the log file for the web viewer (was /log)
async def get_logs_api(lines: int = 300) -> dict:
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        return {"status": "error", "message": "Log file not found.", "log": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-max(1, min(lines, 2000)):]
        return {"status": "success", "log": "".join(tail)}
    except Exception as e:
        return {"status": "error", "message": str(e), "log": ""}


#----- Download the raw log file (was /log document)
async def download_logs_api():
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log file not found.")
    return FileResponse(path, filename="log.txt", media_type="text/plain")


#----- Run the updater then re-exec the app; runs after the HTTP response is flushed
async def _perform_restart(delay: float = 1.0) -> None:
    await asyncio.sleep(delay)
    try:
        LOGGER.info("Web-triggered restart: running updater...")
        proc = await asyncio.create_subprocess_exec("uv", "run", "update.py")
        await proc.wait()
    except Exception as e:
        LOGGER.error(f"Restart updater failed: {e}")

    uv_path = shutil.which("uv")
    if not uv_path:
        LOGGER.error("Restart aborted: uv not found in PATH.")
        return
    LOGGER.info("Web-triggered restart: re-executing app...")
    os.execl(uv_path, uv_path, "run", "-m", "Backend")


#----- Trigger a restart from the web (was /restart)
async def restart_app_api() -> dict:
    asyncio.create_task(_perform_restart())
    return {"status": "success", "message": "Restart initiated — the server will be back shortly."}



