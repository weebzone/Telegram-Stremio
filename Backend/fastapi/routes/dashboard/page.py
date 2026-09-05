from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from Backend import StartTime, __version__, db
from Backend.fastapi.security.credentials import get_current_user, require_auth
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_all_themes, get_all_styles, get_theme
from Backend.helper.system.analytics import get_activity_overview
from Backend.helper.streaming.custom_dl import ACTIVE_STREAMS, RECENT_STREAMS
from Backend.helper.metadata import resolve_cover_url
from Backend.helper.telegram.pyro import get_readable_time
from Backend.pyrofork.bot import multi_clients, work_loads_summary
import time

templates = Jinja2Templates(directory="Backend/fastapi/templates")


def _base_context(request: Request) -> dict:
    theme_name = request.session.get("theme", DEFAULT_THEME)
    style_name = request.session.get("style", DEFAULT_STYLE)
    return {
        "request": request,
        "theme": get_theme(theme_name, style_name),
        "themes": get_all_themes(),
        "styles": get_all_styles(),
        "current_theme": theme_name,
        "current_style": style_name,
    }


async def admin_dashboard_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("admin/admin_dashboard.html", ctx)


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
    try:
        ctx["user_activity_initial"] = await get_activity_overview(1, 5)
    except Exception:
        ctx["user_activity_initial"] = {"users": [], "online_count": 0, "total": 0, "page": 1, "per_page": 5, "total_pages": 1}
    return templates.TemplateResponse("dashboard/dashboard.html", ctx)


