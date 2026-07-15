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
    cancel_duplicate_check_api,
    cancel_scan_api,
    clear_cache_api,
    clear_stream_analytics_api,
    create_custom_catalog_api,
    create_token_api,
    grant_lifetime_api,
    set_token_lifetime_api,
    set_token_expiry_api,
    subscription_preflight_api,
    backfill_subscriber_names_api,
    dbcheck_status_api,
    duplicate_check_status_api,
    delete_custom_catalog_api,
    delete_media_api,
    delete_movie_quality_api,
    delete_request_api,
    delete_subscription_plan_api,
    export_config_api,
    import_config_api,
    delete_tv_episode_api,
    delete_tv_quality_api,
    delete_tv_season_api,
    download_logs_api,
    get_admin_stats_api,
    get_db_stats_api,
    get_all_subscribers_api,
    get_all_tokens_api,
    get_auto_catalog_settings_api,
    get_custom_catalog_items_api,
    get_dead_links_api,
    get_media_visibility_api,
    get_requests_api,
    request_popular_api,
    request_search_api,
    request_submit_api,
    get_stream_analytics_api,
    get_subscription_plans_api,
    get_settings_api,
    get_logs_api,
    get_manual_session_api,
    get_system_stats_api,
    get_tools_channels_api,
    clear_manual_session_api,
    search_manual_session_api,
    set_manual_session_api,
    health_api,
    health_report_api,
    setup_status_api,
    link_token_user_api,
    list_custom_catalogs_api,
    list_media_api,
    manage_subscriber_api,
    manual_add_media_api,
    list_manual_add_catalogs_api,
    resolve_manual_metadata_api,
    purge_dead_links_api,
    purge_duplicates_api,
    remove_custom_catalog_item_api,
    resolve_telegram_api,
    resolve_subtitle_api,
    list_subtitle_languages_api,
    list_subtitles_api,
    add_subtitles_api,
    remove_subtitle_api,
    restart_app_api,
    revoke_token_api,
    scan_status_api,
    search_catalog_media_api,
    set_media_visibility_api,
    search_media_rescan_api,
    speed_test_api,
    speed_test_stream_api,
    start_dbcheck_api,
    start_duplicate_check_api,
    start_scan_api,
    update_auto_catalog_settings_api,
    update_custom_catalog_api,
    update_media_api,
    update_request_api,
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
    admin_requests_page,
    admin_subscriptions_page,
    public_request_page,
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
async def media_management(request: Request, media_type: str = "movie", custom: bool = False, _: bool = Depends(require_auth)):
    return await media_management_page(request, media_type, custom, _)

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
    custom: bool = Query(False),
    _: bool = Depends(require_auth)
):
    return await list_media_api(media_type, page, page_size, search, custom)

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

@app.patch("/api/admin/access/tokens/{token}/lifetime")
async def set_token_lifetime(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await set_token_lifetime_api(token, payload)

@app.post("/api/admin/access/tokens/{token}/expiry")
async def set_token_expiry(token: str, payload: dict, _: bool = Depends(require_auth)):
    return await set_token_expiry_api(token, payload)

@app.post("/api/admin/access/grant-lifetime")
async def grant_lifetime(_: bool = Depends(require_auth)):
    return await grant_lifetime_api()

@app.get("/api/admin/subscriptions/preflight")
async def subscription_preflight(_: bool = Depends(require_auth)):
    return await subscription_preflight_api()

@app.post("/api/admin/subscriptions/backfill-names")
async def backfill_subscriber_names(_: bool = Depends(require_auth)):
    return await backfill_subscriber_names_api()


#----- Public content request page (no auth)
@app.get("/request", response_class=HTMLResponse)
async def public_request(request: Request):
    return await public_request_page(request)

@app.get("/api/request/search")
async def request_search(q: str = Query("")):
    return await request_search_api(q)

@app.get("/api/request/popular")
async def request_popular():
    return await request_popular_api()

@app.post("/api/request/submit")
async def request_submit(payload: dict, request: Request):
    client_ip = request.client.host if request.client else None
    return await request_submit_api(payload, client_ip)


#----- Admin content requests
@app.get("/admin/requests", response_class=HTMLResponse)
async def admin_requests(request: Request, _: bool = Depends(require_auth)):
    return await admin_requests_page(request, _)

@app.get("/api/admin/requests")
async def get_requests(_: bool = Depends(require_auth)):
    return await get_requests_api()

@app.patch("/api/admin/requests/{request_id}")
async def update_request(request_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_request_api(request_id, payload)

@app.delete("/api/admin/requests/{request_id}")
async def delete_request_route(request_id: str, _: bool = Depends(require_auth)):
    return await delete_request_api(request_id)

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


#----- Manual add (custom movie/tv/season/episode/stream)
@app.post("/api/media/resolve-telegram")
async def resolve_telegram(payload: dict, _: bool = Depends(require_auth)):
    return await resolve_telegram_api(payload)

@app.post("/api/media/manual-add")
async def manual_add_media(payload: dict, _: bool = Depends(require_auth)):
    return await manual_add_media_api(payload)

@app.get("/api/media/manual-add/catalogs")
async def manual_add_catalogs(_: bool = Depends(require_auth)):
    return await list_manual_add_catalogs_api()

@app.get("/api/media/manual-add/resolve-meta")
async def manual_add_resolve_meta(media_type: str, selected_id: str, _: bool = Depends(require_auth)):
    return await resolve_manual_metadata_api(media_type, selected_id)


#----- Manual subtitle management
@app.get("/api/media/subtitles/languages")
async def subtitle_languages(_: bool = Depends(require_auth)):
    return list_subtitle_languages_api()

@app.get("/api/media/subtitles")
async def list_subtitles(media_type: str, tmdb_id: int, db_index: int, _: bool = Depends(require_auth)):
    return await list_subtitles_api(media_type, tmdb_id, db_index)

@app.post("/api/media/subtitles/resolve")
async def resolve_subtitle(payload: dict, _: bool = Depends(require_auth)):
    return await resolve_subtitle_api(payload)

@app.post("/api/media/subtitles/add")
async def add_subtitles(payload: dict, _: bool = Depends(require_auth)):
    return await add_subtitles_api(payload)

@app.post("/api/media/subtitles/remove")
async def remove_subtitle_route(payload: dict, _: bool = Depends(require_auth)):
    return await remove_subtitle_api(payload)


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

@app.post("/api/custom-catalogs/media-visibility")
async def set_media_visibility(payload: dict, _: bool = Depends(require_auth)):
    return await set_media_visibility_api(payload)

@app.get("/api/custom-catalogs/media-visibility")
async def get_media_visibility(
    tmdb_id: int,
    db_index: int,
    media_type: str = Query("movie", regex="^(movie|tv|series)$"),
    _: bool = Depends(require_auth)
):
    return await get_media_visibility_api(tmdb_id, db_index, media_type)

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
    force_refresh: bool = Query(False),
    _: bool = Depends(require_auth)
):
    return await auto_sync_custom_catalogs_api(force_refresh)

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


#----- System & Maintenance (WebUI replacement for /stats, /log, /restart bot commands)
@app.get("/api/admin/stats")
async def admin_db_stats(_: bool = Depends(require_auth)):
    return await get_db_stats_api()

@app.get("/api/admin/health")
async def admin_health(_: bool = Depends(require_auth)):
    return await health_api()

@app.get("/api/admin/health/report")
async def admin_health_report(fresh: bool = Query(False), _: bool = Depends(require_auth)):
    return await health_report_api(force=fresh)

@app.get("/api/admin/setup-status")
async def admin_setup_status(_: bool = Depends(require_auth)):
    return await setup_status_api()

@app.get("/api/admin/backup/export")
async def admin_backup_export(_: bool = Depends(require_auth)):
    from fastapi.responses import JSONResponse
    data = await export_config_api()
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": 'attachment; filename="telegram-stremio-backup.json"'},
    )

@app.post("/api/admin/backup/import")
async def admin_backup_import(payload: dict, _: bool = Depends(require_auth)):
    return await import_config_api(payload)

@app.get("/api/admin/logs")
async def admin_logs(lines: int = Query(300, ge=1, le=2000), _: bool = Depends(require_auth)):
    return await get_logs_api(lines)

@app.get("/api/admin/logs/download")
async def admin_logs_download(_: bool = Depends(require_auth)):
    return await download_logs_api()

@app.post("/api/admin/restart")
async def admin_restart(_: bool = Depends(require_auth)):
    return await restart_app_api()


#----- Tools (WebUI replacement for /scan, /rescan, /dbcheck bot commands)
@app.get("/admin/tools", response_class=HTMLResponse)
async def admin_tools(request: Request, _: bool = Depends(require_auth)):
    return await tools_page(request, _)

@app.get("/api/admin/tools/channels")
async def tools_channels(_: bool = Depends(require_auth)):
    return await get_tools_channels_api()

@app.get("/api/admin/tools/manual-session")
async def tools_manual_session_get(_: bool = Depends(require_auth)):
    return await get_manual_session_api()

@app.get("/api/admin/tools/manual-session/search")
async def tools_manual_session_search(query: str = Query(""), _: bool = Depends(require_auth)):
    return await search_manual_session_api(query)

@app.post("/api/admin/tools/manual-session")
async def tools_manual_session_set(payload: dict, _: bool = Depends(require_auth)):
    return await set_manual_session_api(payload)

@app.delete("/api/admin/tools/manual-session")
async def tools_manual_session_clear(_: bool = Depends(require_auth)):
    return await clear_manual_session_api()

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

@app.post("/api/admin/tools/duplicates/start")
async def tools_duplicates_start(_: bool = Depends(require_auth)):
    return await start_duplicate_check_api()

@app.post("/api/admin/tools/duplicates/cancel")
async def tools_duplicates_cancel(_: bool = Depends(require_auth)):
    return await cancel_duplicate_check_api()

@app.get("/api/admin/tools/duplicates/status")
async def tools_duplicates_status(_: bool = Depends(require_auth)):
    return await duplicate_check_status_api()

@app.post("/api/admin/tools/duplicates/purge")
async def tools_duplicates_purge(payload: dict | None = None, _: bool = Depends(require_auth)):
    return await purge_duplicates_api(payload)


@app.exception_handler(401)
async def auth_exception_handler(request: Request, exc):
    return RedirectResponse(url="/login", status_code=302)
