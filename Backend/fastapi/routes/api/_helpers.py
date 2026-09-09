"""
api/_helpers.py — shared helpers for admin API route handlers.

TMDB id coercion, cover URL resolution, limit parsing, scan client picker,
manual-session helpers, bot-admin privilege helpers, placeholder metadata,
visibility cleaning, and restart helper.
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

_PLACEHOLDER_GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy",
                       "Thriller", "Mystery", "Sci-Fi", "Romance", "Family"]
_PLACEHOLDER_DESCRIPTIONS = [
    "A gripping story full of unexpected twists and turns.",
    "An unforgettable journey that keeps you on the edge of your seat.",
    "A captivating tale of drama, courage and emotion.",
    "An entertaining experience packed with memorable moments.",
    "A thrilling adventure blending heart, action and wonder.",
]

_VISIBILITY_MODES = ("public", "tokens", "owner")
_DEFAULT_CATALOG_ENTRIES = [
    {"id": "latest_movies", "name": "Latest Movies", "group": "Default Movies", "type": "movie"},
    {"id": "top_movies", "name": "Popular Movies", "group": "Default Movies", "type": "movie"},
    {"id": "latest_series", "name": "Latest Series", "group": "Default TV", "type": "series"},
    {"id": "top_series", "name": "Popular Series", "group": "Default TV", "type": "series"},
]

LOG_FILE = "log.txt"

def _coerce_tmdb_id(value):
    """Accept int or string; treat 'null'/''/None as missing."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "undefined"):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None

def _require_tmdb_id(value) -> int:
    tid = _coerce_tmdb_id(value)
    if tid is None:
        raise HTTPException(status_code=400, detail="tmdb_id is required and must be an integer")
    return tid

def _resolve_covers(items) -> None:
    for item in items or []:
        for key in ("poster", "backdrop"):
            if item.get(key):
                item[key] = resolve_cover_url(item[key])

def _parse_limit(val):
    try:
        v = float(val)
        return v if v > 0 else None
    except (ValueError, TypeError, AttributeError):
        return None

def _scan_client():
    if StreamBot is not None:
        return StreamBot
    if multi_clients:
        return multi_clients.get(0) or next(iter(multi_clients.values()))
    return None

def _is_personal_media(tmdb_id) -> bool:
    try:
        return int(tmdb_id) < 0
    except (TypeError, ValueError):
        return False

def _session_result(doc: dict) -> dict:
    mt = doc.get("media_type") or doc.get("type") or "movie"
    mt = "tv" if str(mt).lower() in ("tv", "series") else "movie"
    imdb_id = doc.get("imdb_id") or ""
    tmdb_id = doc.get("tmdb_id")
    selected_id = imdb_id if str(imdb_id).startswith("tt") else (str(tmdb_id) if tmdb_id is not None else "")
    return {
        "tmdb_id": tmdb_id,
        "db_index": doc.get("db_index"),
        "media_type": mt,
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or "",
        "poster": resolve_cover_url(doc.get("poster") or ""),
        "imdb_id": imdb_id,
        "selected_id": selected_id,
        "is_personal": _is_personal_media(tmdb_id),
        "in_library": True,
    }

async def _fetch_tg_name(user_id: int):
    try:
        u = await StreamBot.get_users(user_id)
        if not u:
            return None
        name = (u.first_name or "").strip()
        if getattr(u, "last_name", None):
            name = f"{name} {u.last_name}".strip()
        return name or (u.username or None)
    except Exception:
        return None

async def _resolve_speed_test_target(quality_id: str):
    decoded = await decode_string(quality_id)
    target = decoded["parts"][0] if decoded.get("parts") else decoded
    msg_id = target.get("msg_id")
    raw_cid = target.get("chat_id")
    if not msg_id or not raw_cid:
        return None, None, decoded
    return int(f"-100{raw_cid}"), int(msg_id), decoded

def _norm_chat_id(ch):
    s = str(ch).strip()
    if not s:
        return None
    return int(s) if s.lstrip("-").isdigit() else s

async def _managed_bots() -> list[dict]:
    bots: list[dict] = []
    for cid in sorted(multi_clients.keys()):
        client = multi_clients.get(cid)
        if client is None:
            continue
        me = getattr(client, "me", None)
        if me is None:
            try:
                me = await client.get_me()
            except Exception as e:
                LOGGER.warning(f"[BotAdmin] Could not resolve bot client {cid}: {e}")
                me = None
        if not me:
            continue
        bots.append({
            "client_id": cid,
            "user_id": me.id,
            "username": me.username,
            "name": me.first_name or me.username or f"Bot {cid + 1}",
            "is_main": cid == 0,
        })
    return bots

def _bot_served_channels() -> list[dict]:
    s = SettingsManager.current()
    order: list[str] = []
    mapping: dict[str, dict] = {}

    def add(ch, role):
        nid = _norm_chat_id(ch)
        if nid is None:
            return
        key = str(nid)
        if key not in mapping:
            mapping[key] = {"id": nid, "roles": []}
            order.append(key)
        if role not in mapping[key]["roles"]:
            mapping[key]["roles"].append(role)

    for ch in s.auth_channels:
        add(ch, "auth")
    for ch in s.manual_channels:
        add(ch, "manual")
    for ch in s.anime_channels:
        add(ch, "anime")
    if s.announcement_channel:
        add(s.announcement_channel, "announce")
    if s.skip_channel:
        add(s.skip_channel, "skip")
    return [mapping[k] for k in order]

def _bot_admin_privileges() -> ChatPrivileges:
    return ChatPrivileges(
        can_manage_chat=True,
        can_post_messages=True,
        can_edit_messages=True,
        can_delete_messages=True,
        can_invite_users=True,
        can_pin_messages=False,
        can_promote_members=False,
        can_change_info=False,
        can_restrict_members=False,
        can_manage_video_chats=False,
        is_anonymous=False,
    )

def _no_privileges() -> ChatPrivileges:
    return ChatPrivileges(
        can_manage_chat=False,
        can_post_messages=False,
        can_edit_messages=False,
        can_delete_messages=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_promote_members=False,
        can_change_info=False,
        can_restrict_members=False,
        can_manage_video_chats=False,
        is_anonymous=False,
    )

async def _bot_member_status(chat_id, bot_user_id) -> str:
    try:
        m = await botmod.Userbot.get_chat_member(chat_id, bot_user_id)
        st = m.status
        if st in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return "admin"
        if st == ChatMemberStatus.BANNED:
            return "banned"
        if st == ChatMemberStatus.RESTRICTED:
            return "restricted"
        if st == ChatMemberStatus.MEMBER:
            return "member"
        return "missing"
    except Exception:
        return "missing"

def _friendly_promote_error(exc) -> str:
    msg = str(exc)
    up = msg.upper()
    if "CHAT_ADMIN_REQUIRED" in up:
        return "Your session account isn't an admin with rights to do this here."
    if "USER_CREATOR" in up or "ADMIN_RANK" in up:
        return "Can't modify the channel creator."
    if "ADD_ADMINS" in up or ("PROMOTE" in up and "RIGHT" in up):
        return "Your session account can't grant these rights (it doesn't hold them itself)."
    if "PARTICIPANT" in up or "USER_NOT_MUTUAL_CONTACT" in up:
        return "The bot isn't in the channel and couldn't be added automatically."
    if "BOTS_TOO_MUCH" in up:
        return "This channel already has the maximum number of bots."
    return msg

async def _session_rights(chat_id) -> dict:
    try:
        me = await botmod.Userbot.get_chat_member(chat_id, "me")
    except Exception as e:
        return {"manageable": False, "status": "unknown", "reason": f"Couldn't check your rights: {e}"}
    st = me.status
    if st == ChatMemberStatus.OWNER:
        return {"manageable": True, "status": "owner", "reason": ""}
    if st == ChatMemberStatus.ADMINISTRATOR:
        can_promote = bool(getattr(me, "privileges", None) and me.privileges.can_promote_members)
        return {
            "manageable": can_promote,
            "status": "admin_can_promote" if can_promote else "admin_no_promote",
            "reason": "" if can_promote else "You're an admin here but without the 'Add New Admins' permission.",
        }
    return {"manageable": False, "status": "not_admin", "reason": "Your session account is not an admin here."}

async def _promote_one(chat_id, bot: dict, privileges: ChatPrivileges, _retry: bool = True) -> dict:
    label = bot.get("name") or (f"@{bot['username']}" if bot.get("username") else str(bot["user_id"]))
    bid = bot["user_id"]

    if await _bot_member_status(chat_id, bid) == "admin":
        return {"bot": label, "user_id": bid, "status": "already", "message": "Already an admin."}

    try:
        await botmod.Userbot.promote_chat_member(chat_id, bid, privileges=privileges)
        return {"bot": label, "user_id": bid, "status": "added", "message": "Promoted to admin."}
    except FloodWait as fw:
        wait = int(getattr(fw, "value", getattr(fw, "x", 5)) or 5)
        if _retry:
            await asyncio.sleep(wait + 1)
            return await _promote_one(chat_id, bot, privileges, _retry=False)
        return {"bot": label, "user_id": bid, "status": "error",
                "message": f"Rate-limited by Telegram (wait {wait}s) — try again."}
    except Exception as e:
        up = str(e).upper()
        if _retry and ("PARTICIPANT" in up or "USER_NOT_MUTUAL_CONTACT" in up):
            try:
                await botmod.Userbot.add_chat_members(chat_id, bid)
                await asyncio.sleep(0.5)
                await botmod.Userbot.promote_chat_member(chat_id, bid, privileges=privileges)
                return {"bot": label, "user_id": bid, "status": "added", "message": "Added and promoted to admin."}
            except Exception as e2:
                return {"bot": label, "user_id": bid, "status": "error", "message": _friendly_promote_error(e2)}
        return {"bot": label, "user_id": bid, "status": "error", "message": _friendly_promote_error(e)}

async def _demote_one(chat_id, user) -> dict:
    label = getattr(user, "first_name", None) or (f"@{user.username}" if getattr(user, "username", None) else str(user.id))
    try:
        await botmod.Userbot.promote_chat_member(chat_id, user.id, privileges=_no_privileges())
        return {"bot": label, "user_id": user.id, "status": "demoted", "message": "Admin rights removed (orphan)."}
    except Exception as e:
        return {"bot": label, "user_id": user.id, "status": "error", "message": _friendly_promote_error(e)}

async def _run_bot_admin_apply(channel_ids, selected, demote_orphans, managed_ids) -> None:
    state = _bot_admin_apply_state
    privileges = _bot_admin_privileges()
    try:
        for raw in channel_ids:
            cid = _norm_chat_id(raw)
            ch_result = {"id": str(cid), "name": str(cid), "items": []}

            try:
                chat = await botmod.Userbot.get_chat(cid)
                ch_result["name"] = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(cid)
            except Exception as e:
                ch_result["items"].append({"bot": "—", "status": "error", "message": f"Channel not accessible: {e}"})
                state["results"].append(ch_result)
                state["done"] += 1
                continue

            rights = await _session_rights(cid)
            if not rights["manageable"]:
                ch_result["items"].append({
                    "bot": "—", "status": "skipped",
                    "message": rights["reason"] or "Your session account can't add admins here.",
                })
                state["results"].append(ch_result)
                state["done"] += 1
                continue

            for b in selected:
                ch_result["items"].append(await _promote_one(cid, b, privileges))
                await asyncio.sleep(0.3)

            if demote_orphans:
                try:
                    async for m in botmod.Userbot.get_chat_members(cid, filter=ChatMembersFilter.ADMINISTRATORS):
                        u = getattr(m, "user", None)
                        if u and getattr(u, "is_bot", False) and u.id not in managed_ids:
                            ch_result["items"].append(await _demote_one(cid, u))
                            await asyncio.sleep(0.3)
                except Exception as e:
                    ch_result["items"].append({"bot": "orphans", "status": "error", "message": f"Couldn't scan orphans: {e}"})

            state["results"].append(ch_result)
            state["done"] += 1

        state["status"] = "completed"
    except Exception as e:
        LOGGER.error(f"[BotAdmin] Apply run failed: {e}")
        state["status"] = "error"
        state["error"] = str(e)
    finally:
        state["running"] = False

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

async def _set_online_manual_session(payload: dict, media_type: str, selected_id: str) -> dict:
    if not selected_id:
        raise HTTPException(status_code=400, detail="A library title or a selected id is required.")

    meta = await (
        fetch_selected_movie_metadata(selected_id) if media_type == "movie"
        else fetch_selected_tv_metadata(selected_id)
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")

    imdb_id = meta.get("imdb_id") or ""
    default_id = imdb_id if str(imdb_id).startswith("tt") else selected_id

    season = payload.get("season")
    if media_type == "tv" and season is not None and str(season).strip() != "":
        try:
            season = int(season)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Season must be a number.")
    else:
        season = None

    try:
        display_tmdb = int(meta.get("tmdb_id")) if meta.get("tmdb_id") is not None else 0
    except (TypeError, ValueError):
        display_tmdb = 0

    session = {
        "tmdb_id": display_tmdb,
        "db_index": None,
        "media_type": media_type,
        "title": meta.get("title") or "",
        "year": meta.get("release_year") or "",
        "is_personal": False,
        "kind": "real",
        "default_id": default_id,
        "season": season,
        "episode": None,
        "quality": None,
    }
    Backend.MANUAL_SESSION = session
    return {"status": "success", "session": session}

def _fill_placeholder_metadata(meta: dict) -> None:
    title = meta.get("title") or "Media"
    if not meta.get("poster"):
        meta["poster"] = gradient_cover_path(title, portrait=True)
    if not meta.get("backdrop"):
        meta["backdrop"] = gradient_cover_path(title)
    if not meta.get("genres"):
        meta["genres"] = random.sample(_PLACEHOLDER_GENRES, random.randint(1, 3))
    if not meta.get("rate"):
        meta["rate"] = round(random.uniform(6.0, 8.9), 1)
    if not meta.get("description"):
        meta["description"] = random.choice(_PLACEHOLDER_DESCRIPTIONS)

def _clean_visibility(payload: dict):
    visibility = payload.get("visibility")
    if visibility not in _VISIBILITY_MODES:
        visibility = None
    tokens = payload.get("allowed_tokens")
    tokens = [str(t).strip() for t in tokens if str(t).strip()] if isinstance(tokens, list) else []
    return visibility, tokens

def _normalize_media_type(media_type: str) -> str:
    return "tv" if media_type in ["tv", "series"] else "movie"

def _metadata_base(source: dict, from_doc: bool = False) -> dict:
    genres = source.get("genres")
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",") if g.strip()]
    year = source.get("release_year") if from_doc else source.get("year")
    rate = source.get("rating") if from_doc else source.get("rate")
    return {
        "tmdb_id": source.get("tmdb_id"),
        "imdb_id": source.get("imdb_id") or None,
        "title": (source.get("title") or "").strip(),
        "year": int(year) if str(year or "").strip().lstrip("-").isdigit() else 0,
        "rate": float(rate) if str(rate or "").replace(".", "", 1).isdigit() else 0,
        "description": source.get("description") or "",
        "poster": source.get("poster") or "",
        "backdrop": source.get("backdrop") or "",
        "logo": source.get("logo") or "",
        "genres": genres or [],
        "cast": source.get("cast") or [],
        "runtime": str(source.get("runtime") or ""),
        "original_language": source.get("original_language"),
        "origin_country": source.get("origin_country") or [],
    }

_PLACEHOLDER_GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy",
                       "Thriller", "Mystery", "Sci-Fi", "Romance", "Family"]
_PLACEHOLDER_DESCRIPTIONS = [
    "A gripping story full of unexpected twists and turns.",
    "An unforgettable journey that keeps you on the edge of your seat.",
    "A captivating tale of drama, courage and emotion.",
    "An entertaining experience packed with memorable moments.",
    "A thrilling adventure blending heart, action and wonder.",
]

async def _resolve_imdb_id(media_type: str, tmdb_id, db_index) -> str:
    tmdb_id = _coerce_tmdb_id(tmdb_id)
    if not (tmdb_id and db_index):
        raise HTTPException(status_code=400, detail="tmdb_id and db_index are required.")
    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc or not doc.get("imdb_id"):
        raise HTTPException(status_code=404, detail="Title not found.")
    return doc["imdb_id"]
