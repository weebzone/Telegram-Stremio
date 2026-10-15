"""Multi-admin support for the web UI.

Admins live in MongoDB (db "tracking", collection "admin_users"). Each doc:
    { _id, username, password_hash, role, api_token, invited_by,
      created_at, active }

Roles:
    - "owner":  the single supreme admin (migrated from settings on first boot).
                Can manage other admins and all settings.
    - "admin":  invited sub-admin. Operational access only (see credentials.py
                require_role). Cannot touch security/deploy or other admins.

Invite codes (db "tracking", collection "invite_codes"):
    { _id, code, role, created_by, created_at, expires_at, used_by, used_at }
One-time use, expire after INVITE_TTL_SECONDS.

The current admin_username/admin_password in settings is ALWAYS accepted as a
fallback (see credentials.verify_credentials) so the owner never loses access
even if admin_users is empty or a migration fails.
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Dict, List, Optional

from Backend.config import Telegram
from Backend.helper.passwords import hash_password, verify_password
from Backend.logger import LOGGER

INVITE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

_COLL = "admin_users"
_INVITE_COLL = "invite_codes"


def _coll():
    from Backend import db
    return db.dbs["tracking"][_COLL]


def _invite_coll():
    from Backend import db
    return db.dbs["tracking"][_INVITE_COLL]


def _gen_token() -> str:
    return secrets.token_urlsafe(32)


#----- Ensure the settings-based owner exists as an admin_users doc --------
async def ensure_owner() -> None:
    """Create the owner admin from settings if no admins exist yet.

    Safe to call on every startup: it only acts when the collection is empty,
    so it can never clobber existing admins or lock the owner out.
    """
    try:
        if await _coll().count_documents({}) > 0:
            return
        username = (Telegram.ADMIN_USERNAME or "admin").strip() or "admin"
        password_hash = Telegram.ADMIN_PASSWORD  # may be plaintext or hashed
        # Normalize to a hash so we never store a raw password.
        if not (isinstance(password_hash, str) and password_hash.startswith("pbkdf2_sha256$")):
            password_hash = hash_password(password_hash or "admin")
        await _coll().insert_one({
            "username": username,
            "password_hash": password_hash,
            "role": "owner",
            "api_token": _gen_token(),
            "invited_by": None,
            "created_at": time.time(),
            "active": True,
        })
        LOGGER.info(f"[ADMIN] Owner admin '{username}' ensured from settings.")
    except Exception as e:
        LOGGER.error(f"[ADMIN] ensure_owner failed (settings fallback still works): {e}")


#----- Lookups --------------------------------------------------------------
async def get_admin(username: str) -> Optional[Dict[str, Any]]:
    if not username:
        return None
    return await _coll().find_one({"username": username})


async def list_admins() -> List[Dict[str, Any]]:
    out = []
    async for doc in _coll().find({}).sort("created_at", 1):
        out.append({
            "username": doc.get("username"),
            "role": doc.get("role", "admin"),
            "active": bool(doc.get("active", True)),
            "created_at": doc.get("created_at"),
            "invited_by": doc.get("invited_by"),
        })
    return out


#----- Invite codes ---------------------------------------------------------
async def create_invite(created_by: str, role: str = "admin") -> str:
    code = secrets.token_urlsafe(16)
    now = time.time()
    await _invite_coll().insert_one({
        "code": code,
        "role": role if role in ("admin", "owner") else "admin",
        "created_by": created_by,
        "created_at": now,
        "expires_at": now + INVITE_TTL_SECONDS,
        "used_by": None,
        "used_at": None,
    })
    return code


async def _find_invite(code: str) -> Optional[Dict[str, Any]]:
    if not code:
        return None
    return await _invite_coll().find_one({"code": code})


async def consume_invite(code: str) -> Optional[Dict[str, Any]]:
    """Return the invite if valid (exists, unused, unexpired), else None."""
    inv = await _find_invite(code)
    if not inv:
        return None
    if inv.get("used_by"):
        return None
    if (inv.get("expires_at") or 0) < time.time():
        return None
    return inv


async def revoke_invite(code: str) -> bool:
    res = await _invite_coll().delete_one({"code": code, "used_by": None})
    return res.deleted_count > 0


#----- Mutations ------------------------------------------------------------
async def register_admin(username: str, password: str, invite_code: str) -> Dict[str, Any]:
    """Create a new admin using a one-time invite code. Raises ValueError."""
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required.")
    inv = await consume_invite(invite_code)
    if not inv:
        raise ValueError("Invalid, used, or expired invite code.")
    if await get_admin(username):
        raise ValueError("That username is already taken.")

    doc = {
        "username": username,
        "password_hash": hash_password(password),
        "role": inv.get("role", "admin"),
        "api_token": _gen_token(),
        "invited_by": inv.get("created_by"),
        "created_at": time.time(),
        "active": True,
    }
    await _coll().insert_one(doc)
    await _invite_coll().update_one(
        {"_id": inv["_id"]},
        {"$set": {"used_by": username, "used_at": time.time()}},
    )
    return {"username": username, "role": doc["role"]}


async def update_own_password(username: str, new_password: str) -> None:
    """An admin changes their OWN password. Caller must verify it's their session."""
    if not new_password:
        raise ValueError("Password required.")
    await _coll().update_one(
        {"username": username},
        {"$set": {"password_hash": hash_password(new_password)}},
    )


async def owner_reset_password(target_username: str, new_password: str) -> None:
    """Owner forcibly resets another admin's password."""
    if not new_password:
        raise ValueError("Password required.")
    res = await _coll().update_one(
        {"username": target_username},
        {"$set": {"password_hash": hash_password(new_password)}},
    )
    if res.matched_count == 0:
        raise ValueError("Admin not found.")


async def owner_regenerate_token(target_username: str) -> str:
    token = _gen_token()
    res = await _coll().update_one(
        {"username": target_username},
        {"$set": {"api_token": token}},
    )
    if res.matched_count == 0:
        raise ValueError("Admin not found.")
    return token


async def owner_delete_admin(target_username: str) -> None:
    """Owner removes an admin. The owner can never be deleted."""
    admin = await get_admin(target_username)
    if not admin:
        raise ValueError("Admin not found.")
    if admin.get("role") == "owner":
        raise ValueError("The owner account cannot be deleted.")
    await _coll().delete_one({"username": target_username})


#----- Token auth (for scripts / API access) --------------------------------
async def get_admin_by_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    return await _coll().find_one({"api_token": token, "active": True})
