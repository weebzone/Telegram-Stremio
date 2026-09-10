"""
api/bot_admin.py — promote/demote managed bots in auth channels.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pyrogram.enums import ChatMembersFilter

from Backend.logger import LOGGER
import Backend.pyrofork.bot as botmod

from Backend.fastapi.routes.api._helpers import (
    _managed_bots,
    _bot_served_channels,
    _bot_member_status,
    _session_rights,
    _run_bot_admin_apply,
)

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
