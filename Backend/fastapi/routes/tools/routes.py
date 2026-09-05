from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.telegram.pyro import get_scan_client
from Backend.helper.tools.bot_admin import (
    bot_admin_apply,
    bot_admin_apply_status,
    bot_admin_scan,
)
from Backend.helper.tools.channels import list_auth_channels
from Backend.helper.tools.dbcheck_manager import dbcheck_manager
from Backend.helper.tools.duplicate_manager import duplicate_manager
from Backend.helper.tools.manual_session import (
    clear_manual_session,
    get_manual_session,
    search_manual_session,
    set_manual_session,
)
from Backend.helper.tools.scan_manager import scan_manager

router = APIRouter(prefix="/api/admin/tools", tags=["tools"])


@router.get("/channels")
async def tools_channels(_: bool = Depends(require_auth)):
    return await list_auth_channels()


@router.get("/bot-admin/scan")
async def tools_bot_admin_scan(_: bool = Depends(require_auth)):
    return await bot_admin_scan()


@router.post("/bot-admin/apply")
async def tools_bot_admin_apply(payload: dict, _: bool = Depends(require_auth)):
    return await bot_admin_apply(payload)


@router.get("/bot-admin/apply/status")
async def tools_bot_admin_apply_status(_: bool = Depends(require_auth)):
    return await bot_admin_apply_status()


@router.get("/manual-session")
async def tools_manual_session_get(_: bool = Depends(require_auth)):
    return await get_manual_session()


@router.get("/manual-session/search")
async def tools_manual_session_search(query: str = Query(""), _: bool = Depends(require_auth)):
    return await search_manual_session(query)


@router.post("/manual-session")
async def tools_manual_session_set(payload: dict, _: bool = Depends(require_auth)):
    return await set_manual_session(payload)


@router.delete("/manual-session")
async def tools_manual_session_clear(_: bool = Depends(require_auth)):
    return await clear_manual_session()


@router.post("/scan/start")
async def tools_scan_start(payload: dict, _: bool = Depends(require_auth)):
    client = get_scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    mode = str(payload.get("mode", "scan")).lower()
    if mode not in ("scan", "rescan"):
        raise HTTPException(status_code=400, detail="mode must be 'scan' or 'rescan'.")
    channels = payload.get("channels") or []
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="'channels' must be a list.")
    result = await scan_manager.start(client, channels, mode=mode)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start scan."))
    return {"status": "success", **result}


@router.post("/scan/cancel")
async def tools_scan_cancel(_: bool = Depends(require_auth)):
    result = await scan_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}


@router.get("/scan/status")
async def tools_scan_status(_: bool = Depends(require_auth)):
    return {"status": "success", "data": scan_manager.get_status()}


@router.post("/dbcheck/start")
async def tools_dbcheck_start(_: bool = Depends(require_auth)):
    client = get_scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    result = await dbcheck_manager.start(client)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start DB check."))
    return {"status": "success", **result}


@router.post("/dbcheck/cancel")
async def tools_dbcheck_cancel(_: bool = Depends(require_auth)):
    result = await dbcheck_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}


@router.get("/dbcheck/status")
async def tools_dbcheck_status(_: bool = Depends(require_auth)):
    return {"status": "success", "data": dbcheck_manager.get_status()}


@router.post("/dead-links/purge")
async def tools_purge_dead_links(payload: dict | None = None, _: bool = Depends(require_auth)):
    from Backend import db
    payload = payload or {}
    source = str(payload.get("source", "dbcheck")).lower()
    stream_ids = payload.get("stream_ids")

    if stream_ids is not None:
        result = await dbcheck_manager.purge(stream_ids)
    elif source == "flagged":
        try:
            flagged = await db.get_all_dead_links()
            ids = list({d.get("quality_id") for d in flagged if d.get("quality_id")})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not load flagged dead links: {e}")
        result = await dbcheck_manager.purge(ids)
    else:
        result = await dbcheck_manager.purge()

    return {"status": "success" if result.get("ok") else "error", **result}


@router.post("/duplicates/start")
async def tools_duplicates_start(_: bool = Depends(require_auth)):
    result = await duplicate_manager.start()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start duplicate scan."))
    return {"status": "success", **result}


@router.post("/duplicates/cancel")
async def tools_duplicates_cancel(_: bool = Depends(require_auth)):
    result = await duplicate_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}


@router.get("/duplicates/status")
async def tools_duplicates_status(_: bool = Depends(require_auth)):
    return {"status": "success", "data": duplicate_manager.get_status()}


@router.post("/duplicates/purge")
async def tools_duplicates_purge(payload: dict | None = None, _: bool = Depends(require_auth)):
    payload = payload or {}
    delete_all = bool(payload.get("delete_all"))
    stream_ids = payload.get("stream_ids")
    if not delete_all and (not isinstance(stream_ids, list) or not stream_ids):
        raise HTTPException(status_code=400, detail="Provide 'stream_ids' or set 'delete_all'.")
    result = await duplicate_manager.purge(stream_ids, delete_all=delete_all)
    return {"status": "success" if result.get("ok") else "error", **result}
