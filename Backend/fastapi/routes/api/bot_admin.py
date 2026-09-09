"""
api/bot_admin.py — promote/demote managed bots in auth channels.
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

async def bot_admin_scan_api() -> dict:
    if botmod.Userbot is None:
        return {"status": "error", "reason": "no_session",
                "message": "Connect your Telegram session from the Settings page to manage channel admins."}

    bots = await _managed_bots()
    if len(bots) <= 1:
        return {"status": "error", "reason": "single_token", "bots": bots,
                "message": "Add at least one extra bot token (multi-token) to use this tool."}

    channels = _bot_served_channels()
    managed_ids = {b["user_id"] for b in bots}
    out: list[dict] = []

    for ch in channels:
        cid = ch["id"]
        entry = {
            "id": str(cid), "roles": ch["roles"], "name": str(cid),
            "accessible": False, "manageable": False, "session_status": "",
            "reason": "", "bots": {}, "orphans": [],
        }

        try:
            chat = await botmod.Userbot.get_chat(cid)
            entry["name"] = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(cid)
            entry["accessible"] = True
        except Exception as e:
            entry["reason"] = f"Session account can't access this channel: {e}"
            out.append(entry)
            continue

        rights = await _session_rights(cid)
        entry["manageable"] = rights["manageable"]
        entry["session_status"] = rights["status"]
        entry["reason"] = rights["reason"]

        for b in bots:
            entry["bots"][str(b["user_id"])] = await _bot_member_status(cid, b["user_id"])

        try:
            async for m in botmod.Userbot.get_chat_members(cid, filter=ChatMembersFilter.ADMINISTRATORS):
                u = getattr(m, "user", None)
                if u and getattr(u, "is_bot", False) and u.id not in managed_ids:
                    entry["orphans"].append({
                        "user_id": u.id, "username": u.username,
                        "name": u.first_name or u.username or str(u.id),
                    })
        except Exception as e:
            LOGGER.warning(f"[BotAdmin] Could not list admins for {cid}: {e}")

        out.append(entry)

    return {"status": "success", "data": {"bots": bots, "channels": out}}

async def bot_admin_apply_api(payload: dict | None = None) -> dict:
    if botmod.Userbot is None:
        raise HTTPException(status_code=503, detail="No Telegram session connected. Connect one from Settings.")

    if _bot_admin_apply_state["running"]:
        raise HTTPException(status_code=409, detail="An apply run is already in progress.")

    payload = payload or {}
    channel_ids = payload.get("channel_ids") or []
    if not isinstance(channel_ids, list) or not channel_ids:
        raise HTTPException(status_code=400, detail="Select at least one channel.")

    bots = await _managed_bots()
    if len(bots) <= 1:
        raise HTTPException(status_code=400, detail="Need a session string and more than one bot token.")

    bot_by_id = {str(b["user_id"]): b for b in bots}
    sel_ids = payload.get("bot_ids")
    if isinstance(sel_ids, list) and sel_ids:
        selected = [bot_by_id[str(x)] for x in sel_ids if str(x) in bot_by_id]
    else:
        selected = bots
    if not selected:
        raise HTTPException(status_code=400, detail="No matching bots selected.")

    demote_orphans = bool(payload.get("demote_orphans"))
    managed_ids = {b["user_id"] for b in bots}

    _bot_admin_apply_state.update({
        "running": True,
        "status": "running",
        "total": len(channel_ids),
        "done": 0,
        "results": [],
        "error": "",
    })
    _bot_admin_apply_state["task"] = asyncio.create_task(
        _run_bot_admin_apply(channel_ids, selected, demote_orphans, managed_ids)
    )
    return {"status": "started", "total": len(channel_ids)}

async def bot_admin_apply_status_api() -> dict:
    st = _bot_admin_apply_state
    return {
        "status": "success",
        "data": {
            "running": st["running"],
            "state": st["status"],
            "total": st["total"],
            "done": st["done"],
            "results": st["results"],
            "error": st["error"],
        },
    }
