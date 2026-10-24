"""Admin-management & invite-code API routes (multi-admin feature).

All mutating/security endpoints are guarded by `require_owner`. The invite
registration endpoint is public (no auth) but requires a valid one-time code.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from Backend.fastapi.security.credentials import (
    get_current_user,
    get_current_role,
    require_owner,
    verify_admin_token,
)
from Backend.helper import admin_users

router = APIRouter(prefix="/api/admin", tags=["Admin Management"])


class RegisterBody(BaseModel):
    username: str
    password: str
    invite_code: str


class AdminTargetBody(BaseModel):
    target: str
    new_password: str = ""


class AdminTargetOnlyBody(BaseModel):
    target: str


#----- Owner: generate a one-time invite code -------------------------------
@router.post("/invite/generate")
async def invite_generate(request: Request, _: bool = Depends(require_owner)):
    username = get_current_user(request) or "owner"
    code = await admin_users.create_invite(username, role="admin")
    return {"ok": True, "code": code,
            "note": "One-time use. Expires in 7 days."}


#----- Owner: revoke an unused invite code ----------------------------------
@router.post("/invite/revoke")
async def invite_revoke(request: Request, code: str, _: bool = Depends(require_owner)):
    ok = await admin_users.revoke_invite(code)
    if not ok:
        raise HTTPException(status_code=404, detail="Invite not found or already used.")
    return {"ok": True}


#----- Owner: list admins ---------------------------------------------------
@router.get("/admins")
async def list_admins(request: Request, _: bool = Depends(require_owner)):
    return {"admins": await admin_users.list_admins()}


#----- Owner: reset an admin's password -------------------------------------
@router.post("/admins/reset-password")
async def reset_password(body: AdminTargetBody, _: bool = Depends(require_owner)):
    try:
        await admin_users.owner_reset_password(body.target, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "target": body.target}


#----- Owner: regenerate an admin's API token -------------------------------
@router.post("/admins/regenerate-token")
async def regenerate_token(body: AdminTargetOnlyBody, _: bool = Depends(require_owner)):
    try:
        token = await admin_users.owner_regenerate_token(body.target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "target": body.target, "api_token": token}


#----- Owner: delete an admin -----------------------------------------------
@router.post("/admins/delete")
async def delete_admin(body: AdminTargetOnlyBody, _: bool = Depends(require_owner)):
    try:
        await admin_users.owner_delete_admin(body.target)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "target": body.target}


#----- Public: register a new admin using an invite code --------------------
@router.post("/register")
async def register(body: RegisterBody):
    try:
        result = await admin_users.register_admin(
            body.username, body.password, body.invite_code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


#----- Current admin: change OWN password -----------------------------------
class OwnPasswordBody(BaseModel):
    new_password: str


@router.post("/me/password")
async def change_own_password(body: OwnPasswordBody, request: Request):
    username = get_current_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        await admin_users.update_own_password(username, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


#----- Current admin: view OWN api token (for scripts) -----------------------
@router.get("/me/token")
async def my_token(request: Request):
    username = get_current_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    admin = await admin_users.get_admin(username)
    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found")
    return {"username": username, "api_token": admin.get("api_token", "")}


def _unauth():
    raise HTTPException(status_code=401, detail="Not authenticated")
