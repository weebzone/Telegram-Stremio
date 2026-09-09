"""
api/subscription.py — subscription plans and subscribers.
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
from Backend.helper.tasks.backup import export_config, import_config
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
    _fetch_tg_name,
)

async def subscription_preflight_api() -> dict:
    return {"status": "success", "uncovered": await db.count_uncovered_tokens()}

async def backfill_subscriber_names_api() -> dict:
    users = await db.get_all_subscribers()
    updated = 0
    for u in users:
        uid = u.get("_id")
        if uid is None or (u.get("first_name") or "") != f"User {uid}":
            continue
        name = await _fetch_tg_name(uid)
        if name and name != f"User {uid}":
            await db.update_subscriber_name(uid, name)
            updated += 1
    return {"status": "success", "updated": updated, "message": f"{updated} name(s) updated."}

async def get_subscription_plans_api() -> dict:
    try:
        plans = await db.get_subscription_plans()
        return {"status": "success", "data": plans}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def add_subscription_plan_api(payload: dict) -> dict:
    try:
        days = int(payload.get("days", 0))
        price = float(payload.get("price", 0.0))
        currency = str(payload.get("currency") or "INR").upper().strip()
        if days <= 0 or price < 0:
            raise HTTPException(status_code=400, detail="Invalid plan parameters")

        plan_id = await db.add_subscription_plan(days, price, currency)
        if plan_id:
            return {"status": "success", "message": "Plan added successfully", "plan_id": plan_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to add plan")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def update_subscription_plan_api(plan_id: str, payload: dict) -> dict:
    try:
        days = int(payload.get("days", 0))
        price = float(payload.get("price", 0.0))
        currency = str(payload.get("currency") or "INR").upper().strip()
        if days <= 0 or price < 0:
             raise HTTPException(status_code=400, detail="Invalid plan parameters")

        success = await db.update_subscription_plan(plan_id, days, price, currency)
        if success:
             return {"status": "success", "message": "Plan updated successfully"}
        else:
             raise HTTPException(status_code=404, detail="Plan not found or update failed")
    except HTTPException:
         raise
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))

async def delete_subscription_plan_api(plan_id: str) -> dict:
    try:
        success = await db.delete_subscription_plan(plan_id)
        if success:
            return {"status": "success", "message": "Plan deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Plan not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def get_all_subscribers_api() -> dict:
    try:
        users = await db.get_all_subscribers()
        for u in users:
            u["is_admin"] = db._is_owner(u.get("_id"))
        return {"status": "success", "data": users}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def manage_subscriber_api(user_id: int, payload: dict) -> dict:
    try:
        action = payload.get("action")
        days = int(payload.get("days", 0))

        if action not in ["extend", "reduce", "delete", "remove"]:
            raise HTTPException(status_code=400, detail="Invalid action")

        success = await db.manage_subscriber(user_id, action, days)

        if success and action in ("delete", "remove") and SettingsManager.current().subscription:
            group_id = SettingsManager.current().subscription_group_id
            if group_id:
                try:
                    await StreamBot.ban_chat_member(group_id, user_id)
                    await StreamBot.unban_chat_member(group_id, user_id)
                except Exception as exc:
                    LOGGER.warning(f"Revoke: could not remove user {user_id} from group: {exc}")

        if success:
            try:
                invalidate_membership_cache(user_id)
            except Exception:
                pass

        if success:
            verb = {"extend": "extended", "reduce": "reduced", "delete": "revoked", "remove": "removed"}.get(action, "updated")
            return {"status": "success", "message": f"User subscription {verb} successfully"}
        else:
            raise HTTPException(status_code=404, detail="User not found or update failed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def assign_plan_api(user_id: int, days: int) -> dict:
    try:
        name = await _fetch_tg_name(user_id)
        if days and days > 0:
            result = await db.assign_subscription(user_id, days, name)
        else:
            result = await db.set_user_never_expires(user_id, name)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
