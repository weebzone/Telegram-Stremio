from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from Backend.fastapi.security.credentials import is_authenticated, verify_credentials
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


async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("auth/login.html", _base_context(request))


async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["authenticated"] = True
        request.session["username"] = username
        return RedirectResponse(url="/", status_code=302)
    ctx = _base_context(request)
    ctx["error"] = "Invalid credentials"
    return templates.TemplateResponse("auth/login.html", ctx)


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


async def set_theme(request: Request, theme: str = Form(None), style: str = Form(None)):
    if theme and theme in get_all_themes():
        request.session["theme"] = theme
    if style and style in get_all_styles():
        request.session["style"] = style
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=302)
