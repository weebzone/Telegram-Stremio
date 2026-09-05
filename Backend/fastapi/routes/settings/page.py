from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from Backend import db
from Backend.fastapi.security.credentials import get_current_user, require_auth
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_all_themes, get_all_styles, get_theme
from Backend.helper.settings.manager import SettingsManager
import Backend.pyrofork.bot as botmod

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


async def settings_page(request: Request, _: bool = Depends(require_auth)):
    settings = SettingsManager.current().to_dict()
    settings["admin_password"] = ""
    try:
        settings["database_list"] = db.get_database_list()
    except Exception:
        settings["database_list"] = []

    titles = settings.get("channel_titles") or {}
    if not isinstance(titles, dict):
        titles = {}
    settings["channel_titles"] = {str(k): str(v) for k, v in titles.items() if k and v}

    ctx = _base_context(request)
    ctx.update({
        "current_user": get_current_user(request),
        "settings": settings,
        "userbot_configured": botmod.Userbot is not None,
    })
    return templates.TemplateResponse("settings/settings.html", ctx)
