"""
api/system.py — health, logs, stats, restart, and config import/export.
"""

import asyncio
import json
import os
import random
import secrets
import shutil
from datetime import datetime
from time import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pyrogram.enums import ChatMemberStatus, ChatMembersFilter
from pyrogram.errors import FloodWait
from pyrogram.types import ChatPrivileges

import Backend
from Backend import StartTime, __version__, db
from Backend.fastapi.routes.stream_routes import _streamer_by_client
from Backend.fastapi.routes.stremio_routes import invalidate_membership_cache
from Backend.helper.analytics import get_activity_overview
from Backend.helper.auto_catalog import (
    get_auto_catalog_settings,
    get_auto_catalog_sync_status,
    start_auto_catalog_sync_background,
    start_single_media_catalog_sync,
    update_auto_catalog_settings,
)
from Backend.helper.backup import export_config, import_config
from Backend.helper.streaming.byte_streamer import ByteStreamer
from Backend.helper.streaming.speed_test import _speed_test_single_client, run_speed_test
from Backend.helper.encrypt import decode_string, encode_string
from Backend.helper.health import run_health_checks
from Backend.helper.tools.manual_add import resolve_telegram_message, stamp_caption_by_ref
from Backend.helper.requests_manager import (
    delete_request,
    list_requests,
    popular_pending,
    search_titles,
    set_status,
    submit_request,
)
from Backend.helper.metadata import (
    extract_default_id,
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    gradient_cover_path,
    resolve_cover_url,
    search_any_candidates,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.passwords import hash_password, verify_password
from Backend.helper.pyro import get_readable_file_size, get_readable_time
from Backend.helper.tools import dbcheck_manager, duplicate_manager, scan_manager
from Backend.helper.session_auth import (
    disconnect_session,
    get_session_status,
    reconnect_session,
    remove_session,
    start_login,
    submit_code,
    submit_password,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.split_files import strip_part_suffix
from Backend.helper.subtitles import (
    list_languages,
    list_title_subtitles,
    manual_ingest_subtitle,
    remove_subtitle,
    resolve_subtitle_message,
)
from Backend.logger import LOGGER
import Backend.pyrofork.bot as botmod
from Backend.helper.announcer import delete_announcement_async
from Backend.pyrofork.bot import (
    StreamBot,
    client_avg_mbps,
    client_dc_map,
    client_failures,
    multi_clients,
    work_loads,
)

from Backend.fastapi.routes.api._helpers import (
    LOG_FILE,
    _perform_restart,
)

async def get_system_stats_api():
    try:
        db_stats = await db.get_database_stats()
        total_movies, total_tv_shows = db.content_totals(db_stats)
        api_tokens = await db.get_all_api_tokens()

        return {
            "server_status": "running",
            "uptime": get_readable_time(time() - StartTime),
            "telegram_bot": f"@{StreamBot.username}" if StreamBot and StreamBot.username else "@StreamBot",
            "connected_bots": len(multi_clients),
            "version": __version__,
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "databases": db_stats,
            "total_databases": len(db_stats),
            "current_db_index": db.current_db_index,
            "api_tokens": api_tokens
        }
    except Exception as e:
        print(f"System Stats API Error: {e}")
        return {
            "server_status": "error",
            "error": str(e)
        }

async def get_admin_stats_api() -> dict:
    cache_size = sum(len(s._file_id_cache) for s in _streamer_by_client.values())

    bot_stats = []
    for client_index in multi_clients:
        load = work_loads.get(client_index, 0)
        failures = client_failures.get(client_index, 0)
        mbps = client_avg_mbps.get(client_index, 0.0)

        status = "healthy"
        if failures > 5:
            status = "degraded"
        if failures > 15:
            status = "failing"

        bot_stats.append({
            "client_index": client_index,
            "display_name": "Userbot" if client_index < 0 else f"Bot {client_index + 1}",
            "dc": client_dc_map.get(client_index),
            "current_load": load,
            "failures": failures,
            "avg_mbps": round(mbps, 2),
            "status": status
        })

    return {
        "cache_size": cache_size,
        "total_bots": len(multi_clients),
        "bot_workloads": bot_stats
    }

async def clear_cache_api() -> dict:
    total_cleared = sum(len(s._file_id_cache) for s in _streamer_by_client.values())
    for streamer in _streamer_by_client.values():
        streamer._file_id_cache.clear()
    LOGGER.info(f"Admin cleared the FileId cache ({total_cleared} items purged across {len(_streamer_by_client)} clients).")

    return {"status": "success", "message": f"{total_cleared} cached items cleared."}

async def get_stream_analytics_api() -> dict:
    try:
        data = await db.get_stream_analytics(limit=200)
        return {"status": "success", "data": data}
    except Exception as e:
        LOGGER.error(f"Stream analytics API error: {e}")
        return {"status": "error", "message": str(e)}

async def clear_stream_analytics_api() -> dict:
    try:
        result = await db.dbs["tracking"]["stream_analytics"].delete_many({})
        LOGGER.info(f"Admin cleared stream analytics ({result.deleted_count} records deleted).")

        return {
            "status": "success",
            "message": f"{result.deleted_count} analytics records cleared."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

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

async def setup_status_api() -> dict:
    s = SettingsManager.current()
    checks = [
        {"key": "tmdb", "label": "TMDB API key", "done": bool(s.tmdb_api),
         "hint": "Powers automatic poster & metadata matching."},
        {"key": "tvdb", "label": "TVDB API key", "done": bool(s.tvdb_api),
         "hint": "Improves TV show matching; used after TMDB / with anime pipelines."},
        {"key": "channels", "label": "AUTH channel added", "done": len(s.auth_channels) > 0,
         "hint": "The channel(s) the bot indexes and streams from."},
        {"key": "base_url", "label": "Base URL set", "done": bool(s.base_url),
         "hint": "Stremio uses this public address to reach your streams."},
        {"key": "password", "label": "Admin password changed", "done": not verify_password("admin", s.admin_password),
         "hint": "Change the default admin / admin login for security."},
    ]
    done = sum(1 for c in checks if c["done"])
    return {"status": "success", "data": {
        "checks": checks, "done": done, "total": len(checks), "complete": done == len(checks),
    }}

async def export_config_api() -> dict:
    return await export_config()

async def import_config_api(payload: dict) -> dict:
    try:
        result = await import_config(payload)
        return {"status": "success", "result": result, "message": "Backup restored successfully."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOGGER.error(f"Config import error: {e}")
        return {"status": "error", "message": str(e)}

async def health_api() -> dict:
    return {"status": "ok", "start_time": StartTime, "version": __version__}

async def version_status_api(force: bool = False) -> dict:
    from Backend.helper.version_check import check_upstream_version, get_version_status
    if force:
        await check_upstream_version(force=True)
    return {"status": "success", "data": get_version_status()}

async def health_report_api(force: bool = False) -> dict:
    try:
        return {"status": "success", "data": await run_health_checks(force=force)}
    except Exception as e:
        LOGGER.error(f"Health report error: {e}")
        return {"status": "error", "message": str(e)}

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

async def download_logs_api():
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log file not found.")
    return FileResponse(path, filename="log.txt", media_type="text/plain")

async def restart_app_api() -> dict:
    asyncio.create_task(_perform_restart())
    return {"status": "success", "message": "Restart initiated — the server will be back shortly."}

_bot_admin_apply_state: dict = {
    "running": False,
    "status": "idle",
    "total": 0,
    "done": 0,
    "results": [],
    "error": "",
    "task": None,
}

async def get_user_activity_api(page: int = 1, per_page: int = 5):
    try:
        return await get_activity_overview(page, per_page)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
