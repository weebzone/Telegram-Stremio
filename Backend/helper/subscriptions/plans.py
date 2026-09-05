from __future__ import annotations

from fastapi import HTTPException

from Backend import db
from Backend.fastapi.routes.stremio_routes import invalidate_membership_cache
from Backend.helper.access.tokens import fetch_tg_name
from Backend.helper.settings.manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot


async def subscription_preflight() -> dict:
    return {"status": "success", "uncovered": await db.count_uncovered_tokens()}


#----- Relabel "User <id>" placeholder subscribers with their real Telegram name

async def backfill_subscriber_names() -> dict:
    users = await db.get_all_subscribers()
    updated = 0
    for u in users:
        uid = u.get("_id")
        if uid is None or (u.get("first_name") or "") != f"User {uid}":
            continue
        name = await fetch_tg_name(uid)
        if name and name != f"User {uid}":
            await db.update_subscriber_name(uid, name)
            updated += 1
    return {"status": "success", "updated": updated, "message": f"{updated} name(s) updated."}



async def list_plans() -> dict:
    try:
        plans = await db.get_subscription_plans()
        return {"status": "success", "data": plans}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def add_plan(payload: dict) -> dict:
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

async def update_plan(plan_id: str, payload: dict) -> dict:
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

async def delete_plan(plan_id: str) -> dict:
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

async def list_subscribers() -> dict:
    try:
        users = await db.get_all_subscribers()
        for u in users:
            u["is_admin"] = db._is_owner(u.get("_id"))
        return {"status": "success", "data": users}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def manage_subscriber(user_id: int, payload: dict) -> dict:
    try:
        action = payload.get("action")
        days = int(payload.get("days", 0))

        if action not in ["extend", "reduce", "delete", "remove"]:
            raise HTTPException(status_code=400, detail="Invalid action")

        success = await db.manage_subscriber(user_id, action, days)

        #----- On revoke/remove, kick the user from the group immediately (ban+unban)
        if success and action in ("delete", "remove") and SettingsManager.current().subscription:
            group_id = SettingsManager.current().subscription_group_id
            if group_id:
                try:
                    await StreamBot.ban_chat_member(group_id, user_id)
                    await StreamBot.unban_chat_member(group_id, user_id)
                except Exception as exc:
                    LOGGER.warning(f"Revoke: could not remove user {user_id} from group: {exc}")

        #----- Reflect the change immediately in the stremio membership cache
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


