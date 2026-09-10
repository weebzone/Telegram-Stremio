"""
api/tokens.py — Stremio access-token administration.
"""

from datetime import datetime

from fastapi import HTTPException

from Backend import db
from Backend.helper.settings_manager import SettingsManager

from Backend.fastapi.routes.api._helpers import (
    _parse_limit,
    _fetch_tg_name,
)

async def create_token_api(payload: dict):
    try:
        token_name = payload.get("name")
        if not token_name:
            raise HTTPException(status_code=400, detail="Token name is required")

        new_token = await db.add_api_token(
            token_name,
            _parse_limit(payload.get("daily_limit_gb")),
            _parse_limit(payload.get("monthly_limit_gb")),
            subscription_exempt=bool(payload.get("subscription_exempt")),
        )
        return new_token
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def set_token_lifetime_api(token: str, payload: dict) -> dict:
    exempt = bool(payload.get("subscription_exempt"))
    if not await db.set_token_lifetime(token, exempt):
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "success", "subscription_exempt": exempt}

async def set_token_expiry_api(token: str, payload: dict) -> dict:
    user_id = payload.get("user_id")
    if user_id not in (None, "", 0, "0"):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid Telegram user id.")
        await link_token_user_api(token, uid)

    action = str(payload.get("action") or "set")
    days = int(payload.get("days") or 0)
    result = await db.update_token_expiry(token, action, days)
    if not result:
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "success", "expires_at": result.get("expires_at").isoformat() if result.get("expires_at") else None}

async def grant_lifetime_api() -> dict:
    count = await db.grant_lifetime_to_unlinked()
    return {"status": "success", "updated": count, "message": f"{count} token(s) marked as lifetime."}

async def update_token_limits_api(token: str, payload: dict):
    try:
        daily_limit = payload.get("daily_limit_gb")
        monthly_limit = payload.get("monthly_limit_gb")

        await db.update_api_token_limits(
            token,
            _parse_limit(daily_limit),
            _parse_limit(monthly_limit)
        )
        return {"message": "Limits updated successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def get_all_tokens_api() -> dict:
    try:
        tokens = await db.get_all_api_tokens()
        now = datetime.utcnow()
        result = []

        subscriber_map = {}
        if SettingsManager.current().subscription:
            try:
                for u in await db.get_all_subscribers():
                    uid = str(u.get("_id"))
                    subscriber_map[uid] = u
            except Exception:
                pass

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

        def build_entry(user_id, user, token_doc):
            token_doc = token_doc or {}
            user_found = bool(user)
            sub_status = user.get("subscription_status") if user else None
            is_admin = bool(token_doc.get("is_admin")) or db._is_owner(user_id)
            lifetime = bool(token_doc.get("subscription_exempt"))
            token_str = token_doc.get("token")

            token_expiry = token_doc.get("expires_at")
            user_sub_expiry = user.get("subscription_expiry") if user else None

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

        for uid_str, u in subscriber_map.items():
            if uid_str in seen_user_ids:
                continue
            result.append(build_entry(u.get("_id"), u, None))

        result.sort(key=lambda x: (x["is_expired"], not x["has_token"]))
        return {"tokens": result, "subscription": sub_on}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def revoke_token_api(token: str) -> dict:
    try:
        success = await db.revoke_api_token(token)
        if success:
            return {"status": "success", "message": "Token revoked."}
        raise HTTPException(status_code=404, detail="Token not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def link_token_user_api(token: str, user_id: int) -> dict:
    try:
        existing = await db.get_api_token_by_user(user_id)
        if existing and existing.get("token") == token:
            return {"status": "success", "message": f"Already linked to user {user_id}."}
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"User {user_id} is already linked to token '{existing.get('name')}'. Unlink or delete that token first.",
            )
        name = await _fetch_tg_name(user_id)
        success = await db.link_token_user(token, user_id, name)
        if success:
            return {"status": "success", "message": f"Token linked to {name or user_id}."}
        raise HTTPException(status_code=404, detail="Token not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
