from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.settings.ops import (
    download_logs_api,
    export_config_api,
    get_db_stats_api,
    get_logs_api,
    get_settings_api,
    health_api,
    health_report_api,
    import_config_api,
    restart_app_api,
    session_disconnect_api,
    session_reconnect_api,
    session_remove_api,
    session_send_code_api,
    session_status_api,
    session_verify_code_api,
    session_verify_password_api,
    update_settings_api,
)

router = APIRouter(tags=["settings"])


@router.get("/api/admin/settings")
async def get_settings(_: bool = Depends(require_auth)):
    return await get_settings_api()


@router.put("/api/admin/settings")
async def update_settings(payload: dict, _: bool = Depends(require_auth)):
    return await update_settings_api(payload)


@router.post("/api/admin/settings/session/send-code")
async def session_send_code(payload: dict, _: bool = Depends(require_auth)):
    return await session_send_code_api(payload)


@router.post("/api/admin/settings/session/verify-code")
async def session_verify_code(payload: dict, _: bool = Depends(require_auth)):
    return await session_verify_code_api(payload)


@router.post("/api/admin/settings/session/verify-password")
async def session_verify_password(payload: dict, _: bool = Depends(require_auth)):
    return await session_verify_password_api(payload)


@router.get("/api/admin/settings/session")
async def session_status(_: bool = Depends(require_auth)):
    return await session_status_api()


@router.post("/api/admin/settings/session/disconnect")
async def session_disconnect(_: bool = Depends(require_auth)):
    return await session_disconnect_api()


@router.post("/api/admin/settings/session/reconnect")
async def session_reconnect(_: bool = Depends(require_auth)):
    return await session_reconnect_api()


@router.delete("/api/admin/settings/session")
async def session_remove(_: bool = Depends(require_auth)):
    return await session_remove_api()


@router.get("/api/admin/stats")
async def db_stats(_: bool = Depends(require_auth)):
    return await get_db_stats_api()


@router.get("/api/admin/backup/export")
async def backup_export(_: bool = Depends(require_auth)):
    from fastapi.responses import JSONResponse
    data = await export_config_api()
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": 'attachment; filename="telegram-stremio-backup.json"'},
    )


@router.post("/api/admin/backup/import")
async def backup_import(payload: dict, _: bool = Depends(require_auth)):
    return await import_config_api(payload)


@router.get("/api/admin/health")
async def health(_: bool = Depends(require_auth)):
    return await health_api()


@router.get("/api/admin/health/report")
async def health_report(force: bool = Query(False), _: bool = Depends(require_auth)):
    return await health_report_api(force)


@router.get("/api/admin/logs")
async def get_logs(lines: int = Query(300, ge=1, le=2000), _: bool = Depends(require_auth)):
    return await get_logs_api(lines)


@router.get("/api/admin/logs/download")
async def download_logs(_: bool = Depends(require_auth)):
    return await download_logs_api()


@router.post("/api/admin/restart")
async def restart_app(_: bool = Depends(require_auth)):
    return await restart_app_api()
