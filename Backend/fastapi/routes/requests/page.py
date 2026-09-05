from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from Backend.fastapi.security.credentials import get_current_user, require_auth
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


async def admin_requests_page(request: Request, _: bool = Depends(require_auth)):
    ctx = _base_context(request)
    ctx["current_user"] = get_current_user(request)
    return templates.TemplateResponse("requests/requests_manage.html", ctx)


async def public_request_page(request: Request):
    return templates.TemplateResponse("requests/request_public.html", _base_context(request))
