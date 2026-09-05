"""Shared template helpers. Page handlers live under routes/<feature>/page.py."""
from fastapi import Request

from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_all_themes, get_all_styles, get_theme
from Backend.helper.metadata import resolve_cover_url
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="Backend/fastapi/templates")
templates.env.globals["cover_url"] = resolve_cover_url


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
