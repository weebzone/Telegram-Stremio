"""Failed-import review endpoints for the uploader role.

A failed import has no media document, so the regular per-title rescan cannot
be used. Instead the uploader picks the correct title and we re-run the normal
metadata -> insert_media pipeline against the original Telegram message.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from Backend import db
from Backend.helper import failed_imports
from Backend.helper.auto_catalog import start_single_media_catalog_sync
from Backend.helper.metadata import metadata
from Backend.helper.pyro import clean_filename, finalize_media_name
from Backend.helper.task_manager import edit_message
from Backend.logger import LOGGER

router = APIRouter(prefix="/api/admin/imports", tags=["Imports"])

_VIDEO_EXT_RE = re.compile(r"(?i)(\.(?:mkv|mp4|avi|m4v|mov|webm|ts|m2ts))")
_QUALITY_RE = re.compile(r"(?i)\b(?:240|360|480|576|720|1080|1440|2160|4320)p?\b")


def _normalize_quality(quality: str | None) -> str:
    """Accept '1080', '1080p', '4k', '1080P' and return a canonical label."""
    value = (quality or "").strip()
    if not value:
        return ""
    match = _QUALITY_RE.search(value)
    if match:
        token = match.group(0).lower()
        return token if token.endswith("p") else f"{token}p"
    if value.lower() in ("4k", "uhd"):
        return "2160p"
    if value.lower() in ("8k",):
        return "4320p"
    if value.lower() in ("hd", "fhd"):
        return "1080p"
    return value


def _inject_quality(text: str, quality: str) -> str:
    """Insert (or replace) the quality token right before the file extension.

    'Movie Name.mkv' -> 'Movie Name 1080p.mkv'. If the text already carries a
    different quality it is replaced, so we never end up with '720p 1080p'.
    """
    if not text or not quality:
        return text

    existing = _QUALITY_RE.search(text)
    if existing:
        token = existing.group(0).lower()
        token = token if token.endswith("p") else f"{token}p"
        if token == quality.lower():
            return text
        head, tail = text[:existing.start()], text[existing.end():]
        text = f"{head} {quality}{tail}"
        text = re.sub(r"\s+", " ", text).strip()
        return re.sub(r"\s+(\.\w{2,5})$", r"\1", text)

    match = _VIDEO_EXT_RE.search(text)
    if match:
        head, tail = text[:match.start()].rstrip(), text[match.start():]
        return f"{head} {quality}{tail}"
    return f"{text.rstrip()} {quality}"


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
    quality: str | None = None


class QualityBody(BaseModel):
    quality: str
    edit_caption: bool = True
    retry: bool = True


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

    quality = _normalize_quality(body.quality) or _normalize_quality(item.get("quality"))
    diag: dict = {}
    info = await metadata(
        clean_filename(title),
        chat_id,
        msg_id,
        override_id=_normalize_selected(selected, body.media_type),
        season_hint=body.season,
        quality_override=quality or None,
        diag=diag,
    )
    if not info:
        if diag.get("reason") == "no_quality":
            raise HTTPException(
                status_code=422,
                detail="This file has no quality. Add a quality (e.g. 1080p) and try again.",
            )
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


#----- Add/repair the quality of a failed import, then retry automatic mapping
@router.post("/{item_id}/quality")
async def set_import_quality(item_id: str, body: QualityBody, request: Request):
    item = await failed_imports.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found.")

    quality = _normalize_quality(body.quality)
    if not quality:
        raise HTTPException(status_code=400, detail="A quality value is required (e.g. 1080p).")

    chat_id = int(item["chat_id"])
    msg_id = int(item["msg_id"])
    file_name = (item.get("file_name") or "").strip()

    await failed_imports.set_quality(item_id, quality)

    # Persist the quality into the Telegram caption so later scans parse it too.
    caption_updated = False
    if body.edit_caption:
        base = (item.get("caption") or file_name or "").strip()
        new_caption = _inject_quality(base, quality)
        if new_caption and new_caption != base:
            try:
                await edit_message(chat_id, msg_id, new_caption)
                caption_updated = True
            except Exception as e:
                LOGGER.warning(f"[imports] caption edit failed for {chat_id}/{msg_id}: {e}")

    # Retry the normal pipeline with the quality now known.
    mapped = None
    if body.retry:
        source = item.get("caption") or file_name
        diag: dict = {}
        info = await metadata(
            clean_filename(source),
            chat_id,
            msg_id,
            quality_override=quality,
            diag=diag,
        )
        if info:
            insert_status: dict = {}
            inserted = await db.insert_media(
                info,
                channel=chat_id,
                msg_id=msg_id,
                size=item.get("size_label") or "",
                raw_size=int(item.get("size_bytes") or 0),
                name=finalize_media_name(source, bool(info.get("group_key"))),
                status=insert_status,
            )
            if inserted:
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
                mapped = {
                    "tmdb_id": info.get("tmdb_id"),
                    "media_type": info.get("media_type"),
                    "db_index": db.current_db_index,
                }
        else:
            LOGGER.info(f"[imports] retry after quality failed ({diag.get('reason')}) for {item_id}")

    if not mapped:
        await failed_imports.set_status(item_id, "pending")

    return {
        "ok": True,
        "quality": quality,
        "caption_updated": caption_updated,
        "mapped": bool(mapped),
        **(mapped or {}),
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
