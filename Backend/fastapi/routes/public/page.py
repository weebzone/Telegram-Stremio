from fastapi import Request
from fastapi.templating import Jinja2Templates

from Backend import db
from Backend.fastapi.security.credentials import is_authenticated
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_all_themes, get_all_styles, get_theme

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


async def public_status_page(request: Request):
    try:
        db_stats = await db.get_database_stats()
        total_movies, total_tv_shows = db.content_totals(db_stats)
        public_stats = {
            "status": "operational",
            "uptime": "99.9%",
            "total_content": total_movies + total_tv_shows,
            "databases_online": len(db_stats),
        }
    except Exception:
        public_stats = {
            "status": "maintenance",
            "uptime": "N/A",
            "total_content": 0,
            "databases_online": 0,
        }

    ctx = _base_context(request)
    ctx["stats"] = public_stats
    ctx["is_authenticated"] = is_authenticated(request)
    return templates.TemplateResponse("public/public_status.html", ctx)


async def stremio_guide_page(request: Request):
    ctx = _base_context(request)
    ctx["is_authenticated"] = is_authenticated(request)
    return templates.TemplateResponse("public/stremio_configure.html", ctx)
