import secrets
import time

from pyrogram import Client
from pyrogram.errors import (
    BadRequest,
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

import Backend.pyrofork.bot as botmod
from Backend import db
from Backend.config import Telegram
from Backend.helper import global_search, task_manager
from Backend.helper.encrypt import decode_string, encode_string
from Backend.logger import LOGGER

_PENDING = {}
_PENDING_TTL = 600


async def _cleanup_pending():
    now = time.time()
    for lid in [k for k, v in _PENDING.items() if now - v["ts"] > _PENDING_TTL]:
        entry = _PENDING.pop(lid, None)
        if entry:
            try:
                await entry["client"].disconnect()
            except Exception:
                pass


def _profile(me) -> dict:
    name = " ".join(p for p in [me.first_name, me.last_name] if p) or "Telegram User"
    phone = me.phone_number or ""
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    return {
        "name": name,
        "username": me.username or "",
        "phone": phone,
        "user_id": me.id,
    }


async def _store_session(session_string: str, profile: dict) -> None:
    encoded = await encode_string(session_string)
    doc = {
        "session": encoded,
        "active": True,
        "created_at": time.time(),
        **profile,
    }
    await db.dbs["tracking"]["state"].update_one(
        {"_id": "user_session"}, {"$set": doc}, upsert=True
    )


async def _read_stored() -> dict:
    return await db.dbs["tracking"]["state"].find_one({"_id": "user_session"}) or {}


async def get_active_session_string() -> str:
    doc = await _read_stored()
    if not doc or not doc.get("active") or not doc.get("session"):
        return ""
    try:
        return await decode_string(doc["session"])
    except Exception:
        return ""


async def _activate(session_string: str) -> None:
    try:
        if botmod.Userbot is None:
            botmod.build_userbot(session_string)
            await botmod.Userbot.start()
            botmod.Userbot.username = getattr(botmod.Userbot.me, "username", None)
            LOGGER.info("Userbot session activated live from stored session.")
        for mod in (global_search, task_manager):
            try:
                mod._userbot_session_dead = False
            except Exception:
                pass
    except Exception as e:
        LOGGER.warning(f"[SESSION] Live Userbot activation failed (restart to apply): {e}")


async def _deactivate() -> None:
    try:
        if botmod.Userbot is not None:
            await botmod.Userbot.stop()
    except Exception:
        pass
    botmod.Userbot = None


async def start_login(phone: str) -> dict:
    await _cleanup_pending()
    phone = (phone or "").strip()
    if not phone:
        raise ValueError("Enter a valid phone number with country code (e.g. +12025550123).")
    if not Telegram.API_ID or not Telegram.API_HASH:
        raise ValueError("API_ID / API_HASH are not configured.")

    client = Client(f"login_{secrets.token_hex(6)}", api_id=Telegram.API_ID, api_hash=Telegram.API_HASH, in_memory=True)
    await client.connect()
    try:
        sent = await client.send_code(phone)
    except (PhoneNumberInvalid, BadRequest):
        await client.disconnect()
        raise ValueError("That phone number was rejected by Telegram. Check the country code and try again.")
    except FloodWait as e:
        await client.disconnect()
        raise ValueError(f"Too many attempts. Try again in {e.value} seconds.")

    login_id = secrets.token_hex(12)
    _PENDING[login_id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "ts": time.time()}
    return {"login_id": login_id}


async def submit_code(login_id: str, code: str) -> dict:
    entry = _PENDING.get(login_id)
    if not entry:
        raise ValueError("Login session expired. Start again.")
    client = entry["client"]
    code = (code or "").strip().replace(" ", "")
    try:
        await client.sign_in(entry["phone"], entry["hash"], code)
    except SessionPasswordNeeded:
        return {"status": "password_needed"}
    except PhoneCodeInvalid:
        raise ValueError("The code you entered is incorrect.")
    except PhoneCodeExpired:
        _PENDING.pop(login_id, None)
        try:
            await client.disconnect()
        except Exception:
            pass
        raise ValueError("The code has expired. Please request a new one.")
    return await _finalize(login_id)


async def submit_password(login_id: str, password: str) -> dict:
    entry = _PENDING.get(login_id)
    if not entry:
        raise ValueError("Login session expired. Start again.")
    client = entry["client"]
    try:
        await client.check_password((password or "").strip())
    except PasswordHashInvalid:
        raise ValueError("Incorrect two-step verification password.")
    return await _finalize(login_id)


async def _finalize(login_id: str) -> dict:
    entry = _PENDING.pop(login_id, None)
    client = entry["client"]
    me = await client.get_me()
    profile = _profile(me)
    session_string = await client.export_session_string()
    try:
        await client.disconnect()
    except Exception:
        pass
    await _store_session(session_string, profile)
    await _activate(session_string)
    return {"status": "ok", "profile": profile}


async def get_session_status() -> dict:
    doc = await _read_stored()
    if not doc:
        return {"connected": False, "profile": None}
    return {
        "connected": bool(doc.get("active")),
        "live": botmod.Userbot is not None,
        "profile": {
            "name": doc.get("name"),
            "username": doc.get("username"),
            "phone": doc.get("phone"),
            "user_id": doc.get("user_id"),
        },
    }


async def disconnect_session() -> dict:
    await db.dbs["tracking"]["state"].update_one({"_id": "user_session"}, {"$set": {"active": False}})
    await _deactivate()
    return {"ok": True}


async def reconnect_session() -> dict:
    session_string = None
    doc = await _read_stored()
    if doc and doc.get("session"):
        try:
            session_string = await decode_string(doc["session"])
        except Exception:
            session_string = None
    if not session_string:
        raise ValueError("No stored session to reconnect.")
    await db.dbs["tracking"]["state"].update_one({"_id": "user_session"}, {"$set": {"active": True}})
    await _activate(session_string)
    return {"ok": True}


async def remove_session() -> dict:
    await _deactivate()
    await db.dbs["tracking"]["state"].delete_one({"_id": "user_session"})
    return {"ok": True}
