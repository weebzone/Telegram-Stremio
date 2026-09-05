from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pyrogram.enums import ChatMemberStatus, ChatMembersFilter
from pyrogram.types import ChatPrivileges

from Backend.helper.settings.manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import multi_clients
import Backend.pyrofork.bot as botmod


_bot_admin_apply_state: dict = {
    "running": False,
    "status": "idle",
    "total": 0,
    "done": 0,
    "results": [],
    "error": "",
    "task": None,
}


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


async def bot_admin_scan() -> dict:
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


async def bot_admin_apply(payload: dict | None = None) -> dict:
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


async def bot_admin_apply_status() -> dict:
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
