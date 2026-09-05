from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from Backend import db
from Backend.fastapi.security.credentials import get_current_user, require_auth
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_all_themes, get_all_styles, get_theme
from Backend.helper.metadata import resolve_cover_url

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


async def media_management_page(
    request: Request, media_type: str = "movie", custom: bool = False, _: bool = Depends(require_auth)
):
    ctx = _base_context(request)
    ctx.update({
        "current_user": get_current_user(request),
        "media_type": media_type,
        "custom": custom,
    })
    return templates.TemplateResponse("media/media_management.html", ctx)


async def edit_media_page(
    request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)
):
    try:
        media_details = await db.get_document(media_type, tmdb_id, db_index)
        if not media_details:
            raise HTTPException(status_code=404, detail="Media not found")
    except HTTPException:
        raise
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
    return templates.TemplateResponse("media/media_edit.html", ctx)
