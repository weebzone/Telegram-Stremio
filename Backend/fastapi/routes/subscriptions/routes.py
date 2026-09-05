from __future__ import annotations

from fastapi import APIRouter, Depends

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.subscriptions.plans import (
    add_plan,
    backfill_subscriber_names,
    delete_plan,
    list_plans,
    list_subscribers,
    manage_subscriber,
    subscription_preflight,
    update_plan,
)

router = APIRouter(prefix="/api/admin/subscriptions", tags=["subscriptions"])


@router.get("/plans")
async def get_subscription_plans(_: bool = Depends(require_auth)):
    return await list_plans()


@router.post("/plans")
async def add_subscription_plan(payload: dict, _: bool = Depends(require_auth)):
    return await add_plan(payload)


@router.put("/plans/{plan_id}")
async def update_subscription_plan(plan_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_plan(plan_id, payload)


@router.delete("/plans/{plan_id}")
async def delete_subscription_plan(plan_id: str, _: bool = Depends(require_auth)):
    return await delete_plan(plan_id)


@router.get("/users")
async def get_subscribers(_: bool = Depends(require_auth)):
    return await list_subscribers()


@router.post("/users/{user_id}/manage")
async def manage_subscriber_route(user_id: int, payload: dict, _: bool = Depends(require_auth)):
    return await manage_subscriber(user_id, payload)


@router.get("/preflight")
async def subscription_preflight_route(_: bool = Depends(require_auth)):
    return await subscription_preflight()


@router.post("/backfill-names")
async def backfill_subscriber_names_route(_: bool = Depends(require_auth)):
    return await backfill_subscriber_names()
