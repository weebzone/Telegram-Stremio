"""
api/requests.py — public media-request flow and admin request inbox.
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

async def request_search_api(q: str) -> dict:
    try:
        return {"status": "success", "data": await search_titles(q)}
    except Exception as e:
        LOGGER.error(f"Request search error: {e}")
        return {"status": "error", "message": str(e), "data": []}

async def request_submit_api(payload: dict, client_ip: str) -> dict:
    result = await submit_request(
        media_type=payload.get("media_type"),
        tmdb_id=payload.get("tmdb_id"),
        imdb_id=payload.get("imdb_id"),
        title=payload.get("title"),
        year=payload.get("year"),
        poster=payload.get("poster"),
        client_ip=client_ip,
    )
    return {"status": "success" if result.get("ok") else "error", **result}

async def request_popular_api() -> dict:
    try:
        return {"status": "success", "data": await popular_pending()}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}

async def get_requests_api() -> dict:
    try:
        return {"status": "success", "data": await list_requests()}
    except Exception as e:
        LOGGER.error(f"Requests API error: {e}")
        return {"status": "error", "message": str(e)}

async def update_request_api(request_id: str, payload: dict) -> dict:
    new_status = str(payload.get("status", "")).strip()
    doc = await set_status(request_id, new_status)
    if not doc:
        raise HTTPException(status_code=404, detail="Request not found or invalid status.")
    return {"status": "success", "data": doc}

async def delete_request_api(request_id: str) -> dict:
    if not await delete_request(request_id):
        raise HTTPException(status_code=404, detail="Request not found.")
    return {"status": "success", "message": "Request deleted."}
