import time

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from Backend import StartTime, __version__, db
from Backend.config import Telegram
from Backend.fastapi.security.credentials import get_current_user, is_authenticated, require_auth, verify_credentials
from Backend.fastapi.themes import DEFAULT_THEME, get_all_themes, get_theme
from Backend.helper.custom_dl import ACTIVE_STREAMS, RECENT_STREAMS
from Backend.helper.pyro import get_readable_time
from Backend.helper.settings_manager import SettingsManager
from Backend.pyrofork.bot import StreamBot, multi_clients, work_loads_summary

templates = Jinja2Templates(directory="Backend/fastapi/templates")


#----- Shared template context (request, theme metadata) for every page
def _base_context(request: Request) -> dict:
    theme_name = request.session.get("theme", DEFAULT_THEME)
    return {
        "request": request,
        "theme": get_theme(theme_name),
        "themes": get_all_themes(),
        "current_theme": theme_name,
    }


#----- Admin dashboard shell
async def admin_dashboard_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("admin_dashboard.html", ctx)


#----- Login form (redirects to home when already authenticated)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", _base_context(request))


#----- Handle login submission
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["authenticated"] = True
        request.session["username"] = username
        return RedirectResponse(url="/", status_code=302)
    ctx = _base_context(request)
    ctx["error"] = "Invalid credentials"
    return templates.TemplateResponse("login.html", ctx)


#----- Clear the session and return to login
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


#----- Persist the chosen theme and return to the referring page
async def set_theme(request: Request, theme: str = Form(...)):
    if theme in get_all_themes():
        request.session["theme"] = theme
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=302)


#----- Main dashboard: aggregate DB stats and live/recent stream telemetry
async def dashboard_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)

    try:
        db_stats = await db.get_database_stats()
        total_movies, total_tv_shows = db.content_totals(db_stats)

        now = time.time()
        PRUNE_SECONDS = 3
        for sid, info in list(ACTIVE_STREAMS.items()):
            status = info.get("status")
            last_ts = info.get("end_ts") or info.get("last_ts") or info.get("start_ts", now)
            if status in ("cancelled", "error", "finished") and (now - last_ts > PRUNE_SECONDS):
                info["duration"] = round(now - info.get("start_ts", now), 1)
                info["stream_id"] = sid
                try:
                    RECENT_STREAMS.appendleft(info)
                    ACTIVE_STREAMS.pop(sid)
                except KeyError:
                    pass

        active_streams_data = [
            {
                "stream_id": stream_id,
                "msg_id": info.get("msg_id"),
                "chat_id": info.get("chat_id"),
                "status": info.get("status", "active"),
                "total_bytes": info.get("total_bytes", 0),
                "avg_mbps": round(info.get("avg_mbps", 0.0), 2),
                "instant_mbps": round(info.get("instant_mbps", 0.0), 2),
                "peak_mbps": round(info.get("peak_mbps", 0.0), 2),
                "client_index": info.get("client_index", 0),
                "dc_id": info.get("dc_id", 0),
                "duration": round(now - info.get("start_ts", now), 1),
                "meta": info.get("meta", {})
            }
            for stream_id, info in ACTIVE_STREAMS.items()
        ]

        system_stats = {
            "server_status": "running",
            "uptime": get_readable_time(now - StartTime),
            "telegram_bot": f"@{StreamBot.username}" if StreamBot and StreamBot.username else "@StreamBot",
            "connected_bots": len(multi_clients),
            "loads": work_loads_summary(),
            "version": __version__,
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "databases": db_stats,
            "total_databases": len(db_stats),
            "current_db_index": db.current_db_index,
            "active_streams": active_streams_data,
            "total_active_streams": len(active_streams_data)
        }

    except Exception as e:
        print(f"Dashboard error: {e}")
        system_stats = {
            "server_status": "error",
            "error": str(e),
            "uptime": "N/A",
            "telegram_bot": "@StreamBot",
            "connected_bots": 0,
            "loads": {},
            "version": __version__,
            "movies": 0,
            "tv_shows": 0,
            "databases": [],
            "total_databases": 0,
            "current_db_index": 1,
            "active_streams": [],
            "total_active_streams": 0
        }

    ctx["system_stats"] = system_stats
    ctx["api_tokens"] = await db.get_all_api_tokens()
    return templates.TemplateResponse("dashboard.html", ctx)


#----- Media management shell (movie/tv)
async def media_management_page(request: Request, media_type: str = "movie", _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    ctx["media_type"] = media_type
    return templates.TemplateResponse("media_management.html", ctx)


#----- Media edit page for a single title
async def edit_media_page(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    try:
        media_details = await db.get_document(media_type, tmdb_id, db_index)
        if not media_details:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    api_tokens = await db.get_all_api_tokens()
    ctx = _base_context(request)
    ctx.update({
        "current_user": get_current_user(request),
        "tmdb_id": tmdb_id,
        "db_index": db_index,
        "media_type": media_type,
        "media_details": media_details,
        "api_token": api_tokens[0].get("token") if api_tokens else None,
    })
    return templates.TemplateResponse("media_edit.html", ctx)


#----- Public status page (no auth)
async def public_status_page(request: Request):
    try:
        db_stats = await db.get_database_stats()
        total_movies, total_tv_shows = db.content_totals(db_stats)
        public_stats = {
            "status": "operational",
            "uptime": "99.9%",
            "total_content": total_movies + total_tv_shows,
            "databases_online": len(db_stats)
        }
    except Exception:
        public_stats = {
            "status": "maintenance",
            "uptime": "N/A",
            "total_content": 0,
            "databases_online": 0
        }

    ctx = _base_context(request)
    ctx["stats"] = public_stats
    ctx["is_authenticated"] = is_authenticated(request)
    return templates.TemplateResponse("public_status.html", ctx)


#----- Stremio setup guide (no auth)
async def stremio_guide_page(request: Request):
    ctx = _base_context(request)
    ctx["is_authenticated"] = is_authenticated(request)
    return templates.TemplateResponse("stremio_guide.html", ctx)


#----- Subscription management shell
async def admin_subscriptions_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("subscriptions_manage.html", ctx)


#----- Access management shell
async def admin_access_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("access_manage.html", ctx)


#----- Custom catalogs shell
async def custom_catalogs_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("custom_catalogs.html", ctx)


#----- Tools shell (WebUI replacement for scan/rescan/dbcheck commands)
async def tools_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("tools.html", ctx)


#----- Settings page with current config and database list
async def settings_page(request: Request, _: bool = Depends(require_auth)):
    settings = SettingsManager.current().to_dict()
    settings["admin_password"] = ""
    try:
        settings["database_list"] = db.get_database_list()
    except Exception:
        settings["database_list"] = []

    ctx = _base_context(request)
    ctx.update({
        "current_user": get_current_user(request),
        "settings": settings,
        "userbot_configured": bool(Telegram.USER_SESSION_STRING and Telegram.USER_SESSION_STRING.strip()),
    })
    return templates.TemplateResponse("settings.html", ctx)
