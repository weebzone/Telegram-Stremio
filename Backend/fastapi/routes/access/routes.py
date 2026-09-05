from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.access.tokens import (
    assign_plan,
    create_token,
    grant_lifetime,
    link_token_user,
    list_tokens,
    revoke_token,
    set_token_expiry,
    set_token_lifetime,
    update_token_limits,
)

router = APIRouter(tags=["access"])


@router.post("/api/tokens")
async def create_token_route(payload: dict, _: bool = Depends(require_auth)):
    return await create_token(payload)


@router.put("/api/tokens/{token}")
async def update_token_route(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_token_limits(token, payload)


@router.delete("/api/tokens/{token}")
async def revoke_token_legacy(token: str, _: bool = Depends(require_auth)):
    return await revoke_token(token)


@router.get("/api/admin/access/tokens")
async def get_access_tokens(_: bool = Depends(require_auth)):
    return await list_tokens()


@router.delete("/api/admin/access/tokens/{token}")
async def delete_access_token(token: str, _: bool = Depends(require_auth)):
    return await revoke_token(token)


@router.post("/api/admin/access/users/{user_id}/assign-plan")
async def assign_access_plan(user_id: int, payload: dict, _: bool = Depends(require_auth)):
    days = int(payload.get("days", 0))
    return await assign_plan(user_id, days)


@router.patch("/api/admin/access/tokens/{token}/link-user")
async def link_token_to_user(token: str, payload: dict, _: bool = Depends(require_auth)):
    user_id = int(payload.get("user_id", 0))
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required.")
    return await link_token_user(token, user_id)


@router.patch("/api/admin/access/tokens/{token}/lifetime")
async def set_token_lifetime_route(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await set_token_lifetime(token, payload)


@router.post("/api/admin/access/tokens/{token}/expiry")
async def set_token_expiry_route(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await set_token_expiry(token, payload)


@router.post("/api/admin/access/grant-lifetime")
async def grant_lifetime_route(_: bool = Depends(require_auth)):
    return await grant_lifetime()
