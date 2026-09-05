from __future__ import annotations

from fastapi import APIRouter, Depends

from Backend.fastapi.security.credentials import require_auth
from Backend.pyrofork.bot import work_loads_summary
from Backend.helper.dashboard.stats import (
    clear_cache_api,
    clear_stream_analytics_api,
    get_admin_stats_api,
    get_dead_links_api,
    get_stream_analytics_api,
    get_system_stats_api,
    get_user_activity_api,
    setup_status_api,
)

router = APIRouter(tags=["dashboard"])


@router.get("/api/system/stats")
async def system_stats(_: bool = Depends(require_auth)):
    return await get_system_stats_api()


@router.get("/api/admin/system-stats")
async def admin_system_stats(_: bool = Depends(require_auth)):
    return await get_admin_stats_api()


@router.post("/api/admin/clear-cache")
async def clear_cache(_: bool = Depends(require_auth)):
    return await clear_cache_api()


@router.get("/api/admin/dead-links")
async def get_dead_links(_: bool = Depends(require_auth)):
    return await get_dead_links_api()


@router.get("/api/admin/stream-analytics")
async def get_stream_analytics(_: bool = Depends(require_auth)):
    return await get_stream_analytics_api()


@router.post("/api/admin/clear-analytics")
async def clear_analytics(_: bool = Depends(require_auth)):
    return await clear_stream_analytics_api()


@router.get("/api/admin/user-activity")
async def get_user_activity(page: int = 1, per_page: int = 5, _: bool = Depends(require_auth)):
    return await get_user_activity_api(page, per_page)


@router.get("/api/admin/setup-status")
async def setup_status(_: bool = Depends(require_auth)):
    return await setup_status_api()


@router.get("/api/system/workloads")
async def get_workloads(_: bool = Depends(require_auth)):
    try:
        return {"loads": work_loads_summary()}
    except Exception:
        return {"loads": {}}
