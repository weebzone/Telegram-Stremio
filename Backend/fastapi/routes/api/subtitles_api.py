"""
api/subtitles_api.py — subtitle list / add / remove / resolve APIs.
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
from Backend.helper.ops.analytics import get_activity_overview
from Backend.helper.media_extras.auto_catalog import (
    get_auto_catalog_settings,
    get_auto_catalog_sync_status,
    start_auto_catalog_sync_background,
    start_single_media_catalog_sync,
    update_auto_catalog_settings,
)
from Backend.helper.tasks.backup import export_config, import_config
from Backend.helper.streaming.byte_streamer import ByteStreamer
from Backend.helper.streaming.speed_test import _speed_test_single_client, run_speed_test
from Backend.helper.security.encrypt import decode_string, encode_string
from Backend.helper.ops.health import run_health_checks
from Backend.helper.tools.manual_add import resolve_telegram_message, stamp_caption_by_ref
from Backend.helper.ops.requests_manager import (
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
from Backend.helper.security.passwords import hash_password, verify_password
from Backend.helper.telegram.pyro import get_readable_file_size, get_readable_time
from Backend.helper.tools import dbcheck_manager, duplicate_manager, scan_manager
from Backend.helper.security.session_auth import (
    disconnect_session,
    get_session_status,
    reconnect_session,
    remove_session,
    start_login,
    submit_code,
    submit_password,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.telegram.split_files import strip_part_suffix
from Backend.helper.media_extras.subtitles import (
    list_languages,
    list_title_subtitles,
    manual_ingest_subtitle,
    remove_subtitle,
    resolve_subtitle_message,
)
from Backend.logger import LOGGER
import Backend.pyrofork.bot as botmod
from Backend.helper.ops.announcer import delete_announcement_async
from Backend.pyrofork.bot import (
    StreamBot,
    client_avg_mbps,
    client_dc_map,
    client_failures,
    multi_clients,
    work_loads,
)

from Backend.fastapi.routes.api._helpers import (
    _require_tmdb_id,
    _scan_client,
    _resolve_imdb_id,
)

async def resolve_subtitle_api(payload: dict) -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    try:
        data = await resolve_subtitle_message(
            client, url=payload.get("url"),
            chat_id=payload.get("chat_id"), msg_id=payload.get("msg_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")
    return {"status": "success", "data": data}

async def list_subtitles_api(media_type: str, tmdb_id, db_index) -> dict:
    tmdb_id = _require_tmdb_id(tmdb_id)
    mt = "tv" if media_type in ("tv", "series") else "movie"
    imdb_id = await _resolve_imdb_id(mt, tmdb_id, db_index)
    return {"status": "success", "subtitles": await list_title_subtitles(imdb_id)}

async def add_subtitles_api(payload: dict) -> dict:
    media_type = "tv" if payload.get("media_type") in ("tv", "series") else "movie"
    imdb_id = await _resolve_imdb_id(media_type, payload.get("tmdb_id"), payload.get("db_index"))
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="Provide at least one subtitle to add.")

    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    added, errors = [], []
    for item in items:
        try:
            season = item.get("season") if media_type == "tv" else None
            episode = item.get("episode") if media_type == "tv" else None
            if media_type == "tv" and (not season or not episode):
                raise ValueError("Season and episode are required for series subtitles.")
            resolved = await resolve_subtitle_message(
                client, url=item.get("url"),
                chat_id=item.get("chat_id"), msg_id=item.get("msg_id"),
            )
            doc = await manual_ingest_subtitle(
                imdb_id, media_type, season, episode,
                item.get("lang_code") or resolved["lang_code"],
                resolved["chat_id"], resolved["msg_id"], resolved["name"],
            )
            added.append({
                "name": doc["name"], "lang_label": doc["lang_label"],
                "season": doc["season"], "episode": doc["episode"],
            })
        except ValueError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"Could not add subtitle: {exc}")

    if not added and errors:
        raise HTTPException(status_code=400, detail=" ".join(errors))
    message = f"Added {len(added)} subtitle(s)."
    if errors:
        message += f" {len(errors)} failed: {' '.join(errors)}"
    return {"status": "success", "message": message, "added": added, "errors": errors}

async def remove_subtitle_api(payload: dict) -> dict:
    chat_id = payload.get("chat_id")
    msg_id = payload.get("msg_id")
    if chat_id in (None, "") or msg_id in (None, ""):
        raise HTTPException(status_code=400, detail="chat_id and msg_id are required.")
    if not await remove_subtitle(chat_id, msg_id):
        raise HTTPException(status_code=404, detail="Subtitle not found.")
    return {"status": "success", "message": "Subtitle removed."}

def list_subtitle_languages_api() -> dict:
    return {"status": "success", "languages": list_languages()}
