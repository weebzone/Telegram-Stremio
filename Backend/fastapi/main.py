import asyncio

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from Backend import __version__
from Backend.fastapi.routes.api_routes import (
    add_custom_catalog_item_api,
    add_subscription_plan_api,
    apply_media_rescan_api,
    assign_plan_api,
    auto_catalog_sync_status_api,
    auto_sync_custom_catalogs_api,
    cancel_dbcheck_api,
    cancel_scan_api,
    clear_cache_api,
    clear_stream_analytics_api,
    create_custom_catalog_api,
    create_token_api,
    dbcheck_status_api,
    delete_custom_catalog_api,
    delete_media_api,
    delete_movie_quality_api,
    delete_subscription_plan_api,
    delete_tv_episode_api,
    delete_tv_quality_api,
    delete_tv_season_api,
    get_admin_stats_api,
    get_all_subscribers_api,
    get_all_tokens_api,
    get_auto_catalog_settings_api,
    get_custom_catalog_items_api,
    get_dead_links_api,
    get_stream_analytics_api,
    get_subscription_plans_api,
    get_settings_api,
    get_system_stats_api,
    get_tools_channels_api,
    link_token_user_api,
    list_custom_catalogs_api,
    list_media_api,
    manage_subscriber_api,
    purge_dead_links_api,
    remove_custom_catalog_item_api,
    revoke_token_api,
    scan_status_api,
    search_catalog_media_api,
    search_media_rescan_api,
    speed_test_api,
    speed_test_stream_api,
    start_dbcheck_api,
    start_scan_api,
    update_auto_catalog_settings_api,
    update_custom_catalog_api,
    update_media_api,
    update_settings_api,
    update_subscription_plan_api,
    update_token_limits_api,
)
from Backend.fastapi.routes.stream_routes import decay_client_failures
from Backend.fastapi.routes.stream_routes import router as stream_router
from Backend.fastapi.routes.stremio_routes import router as stremio_router
from Backend.fastapi.routes.template_routes import (
    admin_access_page,
    admin_dashboard_page,
    admin_subscriptions_page,
    custom_catalogs_page,
    dashboard_page,
    edit_media_page,
    login_page,
    login_post,
    logout,
    media_management_page,
    public_status_page,
    settings_page,
    set_theme,
    stremio_guide_page,
    tools_page,
)
from Backend.fastapi.security.credentials import require_auth
from Backend.pyrofork.bot import work_loads_summary

templates = Jinja2Templates(directory="Backend/fastapi/templates")

app = FastAPI(
    title="Telegram Stremio Media Server",
    description="A powerful, self-hosted Telegram Stremio Media Server built with FastAPI, MongoDB, and PyroFork seamlessly integrated with Stremio for automated media streaming and discovery.",
    version=__version__
)

#----- Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    app.mount("/static", StaticFiles(directory="Backend/fastapi/static"), name="static")
except Exception:
    pass


@app.on_event("startup")
async def _startup():
    asyncio.create_task(decay_client_failures())


#----- Streaming and Stremio routers
app.include_router(stream_router)
app.include_router(stremio_router)


#----- Public routes (no authentication)
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return await login_page(request)

@app.post("/login", response_class=HTMLResponse)
async def login_post_route(request: Request, username: str = Form(...), password: str = Form(...)):
    return await login_post(request, username, password)

@app.get("/logout")
async def logout_route(request: Request):
    return await logout(request)

@app.post("/set-theme")
async def set_theme_route(request: Request, theme: str = Form(...)):
    return await set_theme(request, theme)

@app.get("/status", response_class=HTMLResponse)
async def public_status(request: Request):
    return await public_status_page(request)

@app.get("/stremio", response_class=HTMLResponse)
async def stremio_guide(request: Request):
    return await stremio_guide_page(request)


#----- Protected routes (authentication required)
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, _: bool = Depends(require_auth)):
    return await dashboard_page(request, _)

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: bool = Depends(require_auth)):
    return await admin_dashboard_page(request, _)

@app.get("/media/manage", response_class=HTMLResponse)
async def media_management(request: Request, media_type: str = "movie", _: bool = Depends(require_auth)):
    return await media_management_page(request, media_type, _)

@app.get("/catalogs", response_class=HTMLResponse)
async def custom_catalogs(request: Request, _: bool = Depends(require_auth)):
    return await custom_catalogs_page(request, _)

@app.get("/media/edit", response_class=HTMLResponse)
async def edit_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await edit_media_page(request, tmdb_id, db_index, media_type, _)

@app.get("/api/media/list")
async def list_media(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100),
    _: bool = Depends(require_auth)
):
    return await list_media_api(media_type, page, page_size, search)

@app.delete("/api/media/delete")
async def delete_media(tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await delete_media_api(tmdb_id, db_index, media_type)

@app.put("/api/media/update")
async def update_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await update_media_api(request, tmdb_id, db_index, media_type)

@app.delete("/api/media/delete-quality")
async def delete_movie_quality(tmdb_id: int, db_index: int, id: str, _: bool = Depends(require_auth)):
    return await delete_movie_quality_api(tmdb_id, db_index, id)

@app.delete("/api/media/delete-tv-quality")
async def delete_tv_quality(tmdb_id: int, db_index: int, season: int, episode: int, id: str, _: bool = Depends(require_auth)):
    return await delete_tv_quality_api(tmdb_id, db_index, season, episode, id)

@app.delete("/api/media/delete-tv-episode")
async def delete_tv_episode(tmdb_id: int, db_index: int, season: int, episode: int, _: bool = Depends(require_auth)):
    return await delete_tv_episode_api(tmdb_id, db_index, season, episode)

@app.delete("/api/media/delete-tv-season")
async def delete_tv_season(tmdb_id: int, db_index: int, season: int, _: bool = Depends(require_auth)):
    return await delete_tv_season_api(tmdb_id, db_index, season)

@app.get("/api/system/workloads")
async def get_workloads(_: bool = Depends(require_auth)):
    try:
        return {"loads": work_loads_summary()}
    except Exception:
        return {"loads": {}}

@app.post("/api/tokens")
async def create_token(payload: dict, _: bool = Depends(require_auth)):
    return await create_token_api(payload)

@app.put("/api/tokens/{token}")
async def update_token(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_token_limits_api(token, payload)

@app.delete("/api/tokens/{token}")
async def revoke_token(token: str, _: bool = Depends(require_auth)):
    return await revoke_token_api(token)

@app.get("/api/system/stats")
async def get_system_stats(_: bool = Depends(require_auth)):
    return await get_system_stats_api()

@app.get("/api/admin/system-stats")
async def admin_system_stats(_: bool = Depends(require_auth)):
    return await get_admin_stats_api()

@app.post("/api/admin/clear-cache")
async def clear_cache(_: bool = Depends(require_auth)):
    return await clear_cache_api()

@app.get("/api/admin/dead-links")
async def get_dead_links(_: bool = Depends(require_auth)):
    return await get_dead_links_api()

@app.get("/api/admin/stream-analytics")
async def get_stream_analytics(_: bool = Depends(require_auth)):
    return await get_stream_analytics_api()

@app.post("/api/admin/clear-analytics")
async def clear_analytics(_: bool = Depends(require_auth)):
    return await clear_stream_analytics_api()

@app.get("/admin/subscriptions", response_class=HTMLResponse)
async def admin_subscriptions(request: Request, _: bool = Depends(require_auth)):
    return await admin_subscriptions_page(request, _)

@app.get("/api/admin/subscriptions/plans")
async def get_subscription_plans(_: bool = Depends(require_auth)):
    return await get_subscription_plans_api()

@app.post("/api/admin/subscriptions/plans")
async def add_subscription_plan(payload: dict, _: bool = Depends(require_auth)):
    return await add_subscription_plan_api(payload)

@app.put("/api/admin/subscriptions/plans/{plan_id}")
async def update_subscription_plan(plan_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_subscription_plan_api(plan_id, payload)

@app.delete("/api/admin/subscriptions/plans/{plan_id}")
async def delete_subscription_plan(plan_id: str, _: bool = Depends(require_auth)):
    return await delete_subscription_plan_api(plan_id)

@app.get("/api/admin/subscriptions/users")
async def get_subscribers(_: bool = Depends(require_auth)):
    return await get_all_subscribers_api()

@app.post("/api/admin/subscriptions/users/{user_id}/manage")
async def manage_subscriber(user_id: int, payload: dict, _: bool = Depends(require_auth)):
    return await manage_subscriber_api(user_id, payload)


#----- Access management
@app.get("/admin/access", response_class=HTMLResponse)
async def admin_access(request: Request, _: bool = Depends(require_auth)):
    return await admin_access_page(request, _)

@app.get("/api/admin/access/tokens")
async def get_access_tokens(_: bool = Depends(require_auth)):
    return await get_all_tokens_api()

@app.delete("/api/admin/access/tokens/{token}")
async def delete_access_token(token: str, _: bool = Depends(require_auth)):
    return await revoke_token_api(token)

@app.post("/api/admin/access/users/{user_id}/assign-plan")
async def assign_access_plan(user_id: int, payload: dict, _: bool = Depends(require_auth)):
    days = int(payload.get("days", 0))
    return await assign_plan_api(user_id, days)

@app.patch("/api/admin/access/tokens/{token}/link-user")
async def link_token_to_user(token: str, payload: dict, _: bool = Depends(require_auth)):
    user_id = int(payload.get("user_id", 0))
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required.")
    return await link_token_user_api(token, user_id)

@app.get("/api/system/speedtest")
async def speed_test(
    quality_id: str = Query(...),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(...),
    _: bool = Depends(require_auth)
):
    return await speed_test_api(quality_id, tmdb_id, db_index, media_type)

@app.get("/api/system/speedtest/stream")
async def speed_test_stream(
    quality_id: str = Query(...),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(...),
    _: bool = Depends(require_auth)
):
    return await speed_test_stream_api(quality_id, tmdb_id, db_index, media_type)

@app.get("/api/media/rescan/search")
async def search_media_rescan(
    media_type: str,
    query: str,
    year: int | None = None,
    _: bool = Depends(require_auth)
):
    return await search_media_rescan_api(media_type, query, year)

@app.post("/api/media/rescan/apply")
async def apply_media_rescan(
    request: Request,
    tmdb_id: int,
    db_index: int,
    media_type: str,
    _: bool = Depends(require_auth)
):
    return await apply_media_rescan_api(request, tmdb_id, db_index, media_type)


#----- Custom catalog management
@app.get("/api/custom-catalogs")
async def list_custom_catalogs(
    tmdb_id: int | None = None,
    db_index: int | None = None,
    media_type: str | None = None,
    _: bool = Depends(require_auth)
):
    return await list_custom_catalogs_api(tmdb_id, db_index, media_type)

@app.post("/api/custom-catalogs")
async def create_custom_catalog(payload: dict, _: bool = Depends(require_auth)):
    return await create_custom_catalog_api(payload)

@app.put("/api/custom-catalogs/{catalog_id}")
async def update_custom_catalog(catalog_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_custom_catalog_api(catalog_id, payload)

@app.delete("/api/custom-catalogs/{catalog_id}")
async def delete_custom_catalog(catalog_id: str, _: bool = Depends(require_auth)):
    return await delete_custom_catalog_api(catalog_id)

@app.get("/api/custom-catalogs/search-media")
async def search_catalog_media(
    query: str,
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=50),
    _: bool = Depends(require_auth)
):
    return await search_catalog_media_api(query, media_type, page, page_size)

@app.post("/api/custom-catalogs/auto-sync")
async def auto_sync_custom_catalogs(
    full_rebuild: bool = Query(False),
    _: bool = Depends(require_auth)
):
    return await auto_sync_custom_catalogs_api(full_rebuild)

@app.get("/api/custom-catalogs/auto-sync/status")
async def auto_catalog_sync_status(_: bool = Depends(require_auth)):
    return await auto_catalog_sync_status_api()

@app.get("/api/custom-catalogs/auto-sync/settings")
async def get_auto_catalog_settings_route(_: bool = Depends(require_auth)):
    return await get_auto_catalog_settings_api()

@app.put("/api/custom-catalogs/auto-sync/settings")
async def update_auto_catalog_settings_route(payload: dict, _: bool = Depends(require_auth)):
    return await update_auto_catalog_settings_api(payload)

@app.get("/api/custom-catalogs/{catalog_id}/items")
async def get_custom_catalog_items(
    catalog_id: str,
    media_type: str | None = Query(None, regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    _: bool = Depends(require_auth)
):
    return await get_custom_catalog_items_api(catalog_id, media_type, page, page_size)

@app.post("/api/custom-catalogs/{catalog_id}/items")
async def add_custom_catalog_item(catalog_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await add_custom_catalog_item_api(catalog_id, payload)

@app.delete("/api/custom-catalogs/{catalog_id}/items")
async def remove_custom_catalog_item(
    catalog_id: str,
    tmdb_id: int,
    db_index: int,
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    _: bool = Depends(require_auth)
):
    return await remove_custom_catalog_item_api(catalog_id, tmdb_id, db_index, media_type)


#----- Settings
@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request, _: bool = Depends(require_auth)):
    return await settings_page(request, _)

@app.get("/api/admin/settings")
async def get_settings(_: bool = Depends(require_auth)):
    return await get_settings_api()

@app.put("/api/admin/settings")
async def update_settings(payload: dict, _: bool = Depends(require_auth)):
    return await update_settings_api(payload)


#----- Tools (WebUI replacement for /scan, /rescan, /dbcheck bot commands)
@app.get("/admin/tools", response_class=HTMLResponse)
async def admin_tools(request: Request, _: bool = Depends(require_auth)):
    return await tools_page(request, _)

@app.get("/api/admin/tools/channels")
async def tools_channels(_: bool = Depends(require_auth)):
    return await get_tools_channels_api()

@app.post("/api/admin/tools/scan/start")
async def tools_scan_start(payload: dict, _: bool = Depends(require_auth)):
    return await start_scan_api(payload)

@app.post("/api/admin/tools/scan/cancel")
async def tools_scan_cancel(_: bool = Depends(require_auth)):
    return await cancel_scan_api()

@app.get("/api/admin/tools/scan/status")
async def tools_scan_status(_: bool = Depends(require_auth)):
    return await scan_status_api()

@app.post("/api/admin/tools/dbcheck/start")
async def tools_dbcheck_start(_: bool = Depends(require_auth)):
    return await start_dbcheck_api()

@app.post("/api/admin/tools/dbcheck/cancel")
async def tools_dbcheck_cancel(_: bool = Depends(require_auth)):
    return await cancel_dbcheck_api()

@app.get("/api/admin/tools/dbcheck/status")
async def tools_dbcheck_status(_: bool = Depends(require_auth)):
    return await dbcheck_status_api()

@app.post("/api/admin/tools/dead-links/purge")
async def tools_purge_dead_links(payload: dict | None = None, _: bool = Depends(require_auth)):
    return await purge_dead_links_api(payload)


@app.exception_handler(401)
async def auth_exception_handler(request: Request, exc):
    return RedirectResponse(url="/login", status_code=302)
