from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from Backend import db
from Backend.helper.settings.manager import SettingsManager
from Backend.pyrofork.bot import StreamBot


def parse_limit(val):
    try:
        v = float(val)
        return v if v > 0 else None
    except (ValueError, TypeError, AttributeError):
        return None



async def create_token(payload: dict):
    try:
        token_name = payload.get("name")
        if not token_name:
            raise HTTPException(status_code=400, detail="Token name is required")

        new_token = await db.add_api_token(
            token_name,
            parse_limit(payload.get("daily_limit_gb")),
            parse_limit(payload.get("monthly_limit_gb")),
            subscription_exempt=bool(payload.get("subscription_exempt")),
        )
        return new_token
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Toggle a token's lifetime (subscription-exempt) flag
async def set_token_lifetime(token: str, payload: dict) -> dict:
    exempt = bool(payload.get("subscription_exempt"))
    if not await db.set_token_lifetime(token, exempt):
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "success", "subscription_exempt": exempt}


#----- Set/extend/reduce a token's own expiry (subscription-off mode).
#----- Optionally attach a Telegram user id at the same time.
async def set_token_expiry(token: str, payload: dict) -> dict:
    user_id = payload.get("user_id")
    if user_id not in (None, "", 0, "0"):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid Telegram user id.")
        #----- Enforces one-user-one-token + pulls the real Telegram name
        await link_token_user(token, uid)

    action = str(payload.get("action") or "set")
    days = int(payload.get("days") or 0)
    result = await db.update_token_expiry(token, action, days)
    if not result:
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "success", "expires_at": result.get("expires_at").isoformat() if result.get("expires_at") else None}



#----- Mark all tokens that aren't linked to a user as lifetime
async def grant_lifetime() -> dict:
    count = await db.grant_lifetime_to_unlinked()
    return {"status": "success", "updated": count, "message": f"{count} token(s) marked as lifetime."}

async def update_token_limits(token: str, payload: dict):
    try:
        daily_limit = payload.get("daily_limit_gb")
        monthly_limit = payload.get("monthly_limit_gb")

        await db.update_api_token_limits(
            token,
            parse_limit(daily_limit),
            parse_limit(monthly_limit)
        )
        return {"message": "Limits updated successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Access management
async def list_tokens() -> dict:
    try:
        tokens = await db.get_all_api_tokens()
        now = datetime.utcnow()
        result = []

        #----- Pre-load subscribers keyed by user_id for O(1) lookup
        subscriber_map = {}
        if SettingsManager.current().subscription:
            try:
                for u in await db.get_all_subscribers():
                    uid = str(u.get("_id"))
                    subscriber_map[uid] = u
            except Exception:
                pass

        #----- Display name, preferring a real name/alias over the "User <id>" placeholder
        def display_name(user, user_id, token_name=None):
            placeholder = f"User {user_id}" if user_id is not None else None
            options = [token_name]
            if user:
                options += [user.get("first_name"), user.get("username")]
            for o in options:
                if o and o != placeholder:
                    return o
            for o in options:
                if o:
                    return o
            return placeholder or "Telegram User"

        sub_on = SettingsManager.current().subscription

        #----- Unified access entry from optional user + token records
        def build_entry(user_id, user, token_doc):
            token_doc = token_doc or {}
            user_found = bool(user)
            sub_status = user.get("subscription_status") if user else None
            is_admin = bool(token_doc.get("is_admin")) or db._is_owner(user_id)
            lifetime = bool(token_doc.get("subscription_exempt"))
            token_str = token_doc.get("token")

            token_expiry = token_doc.get("expires_at")
            user_sub_expiry = user.get("subscription_expiry") if user else None

            #----- Sub OFF: token's own expiry (display only). Sub ON: token expiry is an
            #----- admin grant, otherwise fall back to the subscription's expiry.
            if not sub_on:
                expiry = token_expiry
                is_expired = False
            elif is_admin or lifetime:
                expiry = None
                is_expired = False
            elif token_expiry is not None:
                expiry = token_expiry
                is_expired = token_expiry < now
            elif user_found and sub_status == "active" and user_sub_expiry:
                expiry = user_sub_expiry
                is_expired = user_sub_expiry < now
            else:
                expiry = user_sub_expiry
                is_expired = True

            created = token_doc.get("created_at") or (user.get("created_at") if user else None)
            limits = token_doc.get("limits") or {}
            usage = token_doc.get("usage") or {}
            has_active_sub = sub_on and user_found and sub_status == "active" and bool(user_sub_expiry) and user_sub_expiry > now
            never_expires = not expiry and (is_admin or lifetime or not sub_on)

            return {
                "token": token_str,
                "user_id": user_id,
                "user_name": display_name(user, user_id, token_doc.get("name")),
                "user_found": user_found,
                "is_admin": is_admin,
                "lifetime": lifetime,
                "never_expires": never_expires,
                "has_token": bool(token_str),
                "has_active_sub": has_active_sub,
                "created_at": created.isoformat() if created else None,
                "expires_at": expiry.isoformat() if expiry else None,
                "is_expired": is_expired,
                "sub_status": sub_status,
                "daily_limit_gb": limits.get("daily_limit_gb") or 0,
                "monthly_limit_gb": limits.get("monthly_limit_gb") or 0,
                "daily_bytes": (usage.get("daily") or {}).get("bytes", 0),
                "monthly_bytes": (usage.get("monthly") or {}).get("bytes", 0),
                "addon_url": (
                    f"{SettingsManager.current().base_url}/stremio/{token_str}/manifest.json"
                    if token_str else None
                ),
            }

        seen_user_ids = set()

        #----- 1. Process all existing tokens
        for t in tokens:
            token_user_id = t.get("user_id")

            user = None
            if token_user_id:
                uid_str = str(token_user_id)
                user = subscriber_map.get(uid_str)
                if not user:
                    try:
                        user = await db.get_user(int(token_user_id))
                    except Exception:
                        pass
                seen_user_ids.add(uid_str)

            result.append(build_entry(token_user_id, user, t))

        #----- 2. Add subscribers who have no token
        for uid_str, u in subscriber_map.items():
            if uid_str in seen_user_ids:
                continue
            result.append(build_entry(u.get("_id"), u, None))

        #----- Sort: active-with-token first, active-no-token next, expired last
        result.sort(key=lambda x: (x["is_expired"], not x["has_token"]))
        return {"tokens": result, "subscription": sub_on}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def revoke_token(token: str) -> dict:
    try:
        success = await db.revoke_api_token(token)
        if success:
            return {"status": "success", "message": "Token revoked."}
        raise HTTPException(status_code=404, detail="Token not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Assign or extend a subscription for any user_id
async def assign_plan(user_id: int, days: int) -> dict:
    try:
        #----- Use the real Telegram name so the Plans page shows it (not "User <id>")
        name = await fetch_tg_name(user_id)
        #----- 0 / empty days means "never expires"
        if days and days > 0:
            result = await db.assign_subscription(user_id, days, name)
        else:
            result = await db.set_user_never_expires(user_id, name)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Look up a Telegram user's display name via the bot (best-effort)
async def fetch_tg_name(user_id: int):
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


#----- Link an orphan token to a Telegram user_id (one user_id = one token)
async def link_token_user(token: str, user_id: int) -> dict:
    try:
        existing = await db.get_api_token_by_user(user_id)
        if existing and existing.get("token") == token:
            return {"status": "success", "message": f"Already linked to user {user_id}."}
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"User {user_id} is already linked to token '{existing.get('name')}'. Unlink or delete that token first.",
            )
        #----- Overwrite the token name with the user's real Telegram name when available
        name = await fetch_tg_name(user_id)
        success = await db.link_token_user(token, user_id, name)
        if success:
            return {"status": "success", "message": f"Token linked to {name or user_id}."}
        raise HTTPException(status_code=404, detail="Token not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


