"""Failed-import review endpoints for the uploader role.

A failed import has no media document, so the regular per-title rescan cannot
be used. Instead the uploader picks the correct title and we re-run the normal
metadata -> insert_media pipeline against the original Telegram message.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from Backend import db
from Backend.helper import failed_imports
from Backend.helper.auto_catalog import start_single_media_catalog_sync
from Backend.helper.metadata import metadata
from Backend.helper.pyro import clean_filename, finalize_media_name
from Backend.logger import LOGGER

router = APIRouter(prefix="/api/admin/imports", tags=["Imports"])


def _normalize_selected(selected: str, media_type: str) -> str:
    """metadata() extracts ids with `extract_default_id`, which understands
    `tt123`, `mv123`, `tv123` and URLs - but not a bare TMDB number. The
    candidate search returns bare numbers for TMDb results, so prefix them.
    """
    value = (selected or "").strip()
    if value.isdigit():
        return ("tv" if media_type in ("tv", "series") else "mv") + value
    return value


class MapBody(BaseModel):
    selected_id: str
    media_type: str = "movie"
    season: int | None = None
    episode: int | None = None


#----- List failed imports (status=pending on the panel by default)
@router.get("")
async def list_imports(
    status: str = Query("pending"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: str = Query(""),
):
    return await failed_imports.list_items(
        status=status,
        page=page,
        page_size=page_size,
        search=search.strip(),
    )


#----- Map a failed file to the title the uploader picked
@router.post("/{item_id}/map")
async def map_import(item_id: str, body: MapBody, request: Request):
    item = await failed_imports.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found.")

    title = (item.get("file_name") or item.get("caption") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="This import has no filename or caption to parse.")

    selected = (body.selected_id or "").strip()
    if not selected:
        raise HTTPException(status_code=400, detail="selected_id is required.")

    chat_id = int(item["chat_id"])
    msg_id = int(item["msg_id"])

    info = await metadata(
        clean_filename(title),
        chat_id,
        msg_id,
        override_id=_normalize_selected(selected, body.media_type),
        season_hint=body.season,
    )
    if not info:
        raise HTTPException(status_code=422, detail="Could not resolve metadata for that title.")

    insert_status: dict = {}
    inserted = await db.insert_media(
        info,
        channel=chat_id,
        msg_id=msg_id,
        size=item.get("size_label") or "",
        raw_size=int(item.get("size_bytes") or 0),
        name=finalize_media_name(title, bool(info.get("group_key"))),
        status=insert_status,
    )
    if not inserted:
        raise HTTPException(status_code=500, detail="Failed to insert the media into the library.")

    await failed_imports.set_status(
        item_id,
        "fixed",
        fixed_by=_current_user(request),
        tmdb_id=info.get("tmdb_id"),
        db_index=db.current_db_index,
        media_type=info.get("media_type"),
    )
    start_single_media_catalog_sync(
        db,
        tmdb_id=info.get("tmdb_id"),
        media_type=info.get("media_type"),
    )
    LOGGER.info(f"[imports] {item_id} mapped to {info.get('media_type')}/{info.get('tmdb_id')}")

    return {
        "ok": True,
        "tmdb_id": info.get("tmdb_id"),
        "media_type": info.get("media_type"),
        "db_index": db.current_db_index,
    }


#----- Hide an import the uploader decides not to fix
@router.post("/{item_id}/ignore")
async def ignore_import(item_id: str, request: Request):
    item = await failed_imports.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found.")
    await failed_imports.set_status(item_id, "ignored", fixed_by=_current_user(request))
    return {"ok": True}


def _current_user(request: Request) -> str:
    try:
        return request.session.get("username") or ""
    except Exception:
        return ""
