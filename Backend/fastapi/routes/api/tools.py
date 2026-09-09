"""
api/tools.py — Tools page APIs (scan, dbcheck, duplicates, manual session).
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

from Backend.fastapi.routes.api._helpers import *  # noqa: F401,F403

async def get_tools_channels_api() -> dict:
    channels = list(SettingsManager.current().auth_channels)
    client = _scan_client()
    result = []
    for ch in channels:
        name = str(ch)
        try:
            if client is not None:
                chat = await client.get_chat(int(ch) if str(ch).lstrip("-").isdigit() else ch)
                name = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(ch)
        except Exception as e:
            LOGGER.warning(f"[Tools] Could not resolve channel {ch}: {e}")
        result.append({"id": str(ch), "name": name})
    return {"status": "success", "data": result}

async def search_manual_session_api(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"results": []}

    results: list[dict] = []
    seen: set = set()

    def _add(doc: dict) -> None:
        entry = _session_result(doc)
        key = (entry["tmdb_id"], entry["db_index"], entry["media_type"])
        if entry["tmdb_id"] is None or key in seen:
            return
        seen.add(key)
        results.append(entry)

    default_id = extract_default_id(query)
    if default_id:
        try:
            if str(default_id).startswith("tt"):
                doc = await db.get_media_details(default_id)
                if doc:
                    _add(doc)
            else:
                for mt in ("movie", "tv"):
                    location = await db.find_media_doc(mt, int(default_id))
                    if location:
                        found, db_index = location
                        found["media_type"] = mt
                        found["db_index"] = db_index
                        _add(found)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] id lookup failed for '{query}': {e}")

    if not default_id:
        try:
            data = await db.search_documents(query, 1, 20)
            for doc in data.get("results", []):
                _add(doc)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] library search failed for '{query}': {e}")

    library_ids = {(e.get("imdb_id") or "", str(e.get("tmdb_id") or "")) for e in results}
    try:
        online = await search_any_candidates(query)
    except Exception as e:
        LOGGER.warning(f"[Manual Session] online search failed for '{query}': {e}")
        online = []

    for cand in online:
        if not cand.get("selected_id") or not cand.get("title"):
            continue
        imdb_id = cand.get("imdb_id") or ""
        tmdb_id = cand.get("tmdb_id")
        if (imdb_id, str(tmdb_id or "")) in library_ids:
            continue
        results.append({
            "tmdb_id": tmdb_id,
            "db_index": None,
            "media_type": "tv" if cand.get("media_type") == "tv" else "movie",
            "title": cand.get("title") or "",
            "year": cand.get("year") or "",
            "poster": resolve_cover_url(cand.get("poster") or ""),
            "imdb_id": imdb_id,
            "selected_id": str(cand.get("selected_id")),
            "source": cand.get("source"),
            "is_personal": False,
            "in_library": False,
        })

    return {"results": results}

async def get_manual_session_api() -> dict:
    return {"session": getattr(Backend, "MANUAL_SESSION", None)}

async def set_manual_session_api(payload: dict) -> dict:
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = _normalize_media_type(payload.get("media_type", "movie"))
    selected_id = str(payload.get("selected_id") or "").strip()
    in_library = payload.get("in_library", True) and tmdb_id is not None and db_index is not None

    if not in_library:
        return await _set_online_manual_session(payload, media_type, selected_id)

    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc:
        raise HTTPException(status_code=404, detail="That title was not found in your library.")

    is_personal = _is_personal_media(tmdb_id)
    session = {
        "tmdb_id": int(tmdb_id),
        "db_index": int(db_index),
        "media_type": media_type,
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or "",
        "is_personal": is_personal,
    }

    if is_personal:
        season = payload.get("season")
        episode = payload.get("episode")
        quality = str(payload.get("quality") or "").strip()

        if media_type == "tv":
            if season is None or str(season).strip() == "":
                raise HTTPException(status_code=400, detail="A season number is required for personal TV shows.")
            try:
                season = int(season)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Season must be a number.")
            if episode is not None and str(episode).strip() != "":
                try:
                    episode = int(episode)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="Episode must be a number.")
            else:
                episode = None
        else:
            season = None
            episode = None

        session.update({
            "kind": "personal",
            "default_id": None,
            "season": season,
            "episode": episode,
            "quality": quality or None,
        })
    else:
        imdb_id = doc.get("imdb_id") or ""
        default_id = imdb_id if str(imdb_id).startswith("tt") else str(int(tmdb_id))

        season = payload.get("season")
        if media_type == "tv" and season is not None and str(season).strip() != "":
            try:
                season = int(season)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Season must be a number.")
        else:
            season = None

        session.update({
            "kind": "real",
            "default_id": default_id,
            "season": season,
            "episode": None,
            "quality": None,
        })

    Backend.MANUAL_SESSION = session
    return {"status": "success", "session": session}

async def clear_manual_session_api() -> dict:
    Backend.MANUAL_SESSION = None
    return {"status": "success"}

async def start_scan_api(payload: dict) -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    mode = str(payload.get("mode", "scan")).lower()
    if mode not in ("scan", "rescan"):
        raise HTTPException(status_code=400, detail="mode must be 'scan' or 'rescan'.")
    channels = payload.get("channels") or []
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="'channels' must be a list.")

    result = await scan_manager.start(client, channels, mode=mode)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start scan."))
    return {"status": "success", **result}

async def cancel_scan_api() -> dict:
    result = await scan_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}

async def scan_status_api() -> dict:
    return {"status": "success", "data": scan_manager.get_status()}

async def start_dbcheck_api() -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    result = await dbcheck_manager.start(client)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start DB check."))
    return {"status": "success", **result}

async def cancel_dbcheck_api() -> dict:
    result = await dbcheck_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}

async def dbcheck_status_api() -> dict:
    return {"status": "success", "data": dbcheck_manager.get_status()}

async def start_duplicate_check_api() -> dict:
    result = await duplicate_manager.start()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start duplicate scan."))
    return {"status": "success", **result}

async def cancel_duplicate_check_api() -> dict:
    result = await duplicate_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}

async def duplicate_check_status_api() -> dict:
    return {"status": "success", "data": duplicate_manager.get_status()}

async def purge_duplicates_api(payload: dict | None = None) -> dict:
    payload = payload or {}
    delete_all = bool(payload.get("delete_all"))
    stream_ids = payload.get("stream_ids")
    if not delete_all and (not isinstance(stream_ids, list) or not stream_ids):
        raise HTTPException(status_code=400, detail="Provide 'stream_ids' or set 'delete_all'.")
    result = await duplicate_manager.purge(stream_ids, delete_all=delete_all)
    return {"status": "success" if result.get("ok") else "error", **result}

async def purge_dead_links_api(payload: dict | None = None) -> dict:
    payload = payload or {}
    source = str(payload.get("source", "dbcheck")).lower()
    stream_ids = payload.get("stream_ids")

    if stream_ids is not None:
        result = await dbcheck_manager.purge(stream_ids)
    elif source == "flagged":
        try:
            flagged = await db.get_all_dead_links()
            ids = list({d.get("quality_id") for d in flagged if d.get("quality_id")})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not load flagged dead links: {e}")
        result = await dbcheck_manager.purge(ids)
    else:
        result = await dbcheck_manager.purge()

    return {"status": "success" if result.get("ok") else "error", **result}

LOG_FILE = "log.txt"

async def get_dead_links_api() -> dict:
    try:
        dead_links = await db.get_all_dead_links()
        return {"status": "success", "data": dead_links}
    except Exception as e:
        return {"status": "error", "message": str(e)}
