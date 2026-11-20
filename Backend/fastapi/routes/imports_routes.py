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
from Backend.helper.split_files import part_info
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


def _item_part_base(item: dict) -> str:
    base = (item.get("part_base") or "").strip()
    if base:
        return base
    info = part_info(item.get("file_name") or "")
    return info[0] if info else ""


async def _group_targets(item: dict) -> list[dict]:
    """All failed-import rows that belong to the same split file."""
    chat_id = int(item["chat_id"])
    part_base = _item_part_base(item)
    siblings = await failed_imports.list_siblings(chat_id, part_base) if part_base else []
    if not siblings:
        return [item]
    if not any(int(s["msg_id"]) == int(item["msg_id"]) for s in siblings):
        siblings.append(item)
    siblings.sort(key=lambda d: d.get("part_number") or 0)
    return siblings


async def _repair_parts(
    item: dict,
    *,
    selected_id: str | None,
    media_type: str,
    season: int | None,
    quality: str,
    actor: str,
) -> dict:
    """Repair a whole split file: insert every sibling with the same group_key.

    `insert_media` merges parts by group_key, so the result is one quality entry
    with N parts instead of N broken single-part streams.
    """
    chat_id = int(item["chat_id"])
    part_base = _item_part_base(item)
    targets = await _group_targets(item)

    fixed: list[dict] = []
    failed: list[dict] = []
    warnings: list[str] = []
    first_info = None

    for target in targets:
        msg_id = int(target["msg_id"])
        file_name = (target.get("file_name") or "").strip()
        source = clean_filename(target.get("caption") or file_name or "")
        diag: dict = {}

        info = await metadata(
            source,
            chat_id,
            msg_id,
            override_id=selected_id,
            season_hint=season,
            quality_override=quality or None,
            diag=diag,
            file_name=file_name or None,
        )
        if not info:
            failed.append({"id": target.get("id"), "reason": diag.get("reason") or "metadata_failed"})
            await failed_imports.set_status(target["id"], "pending")
            continue

        # A split part without a group_key would become a broken single-part stream.
        if part_base and not info.get("group_key"):
            failed.append({"id": target.get("id"), "reason": "no_group_key"})
            warnings.append("A part could not be linked to its group; skipped to avoid a broken stream.")
            await failed_imports.set_status(target["id"], "pending")
            continue

        insert_status: dict = {}
        inserted = await db.insert_media(
            info,
            channel=chat_id,
            msg_id=msg_id,
            size=target.get("size_label") or "",
            raw_size=int(target.get("size_bytes") or 0),
            name=finalize_media_name(target.get("caption") or file_name, bool(info.get("group_key"))),
            status=insert_status,
        )
        if not inserted:
            failed.append({"id": target.get("id"), "reason": "insert_failed"})
            await failed_imports.set_status(target["id"], "pending")
            continue

        await failed_imports.set_status(
            target["id"],
            "fixed",
            fixed_by=actor,
            tmdb_id=info.get("tmdb_id"),
            db_index=db.current_db_index,
            media_type=info.get("media_type"),
        )
        fixed.append({"id": target.get("id"), "msg_id": msg_id})
        if first_info is None:
            first_info = info

    if first_info:
        start_single_media_catalog_sync(
            db,
            tmdb_id=first_info.get("tmdb_id"),
            media_type=first_info.get("media_type"),
        )

    numbers = sorted({int(t.get("part_number")) for t in targets if t.get("part_number")})
    if numbers:
        missing = [n for n in range(numbers[0], numbers[-1] + 1) if n not in numbers]
        if missing:
            warnings.append("Missing parts: " + ", ".join(str(n) for n in missing))

    return {
        "ok": bool(fixed),
        "parts_total": len(targets),
        "parts_fixed": len(fixed),
        "parts_failed": len(failed),
        "fixed": fixed,
        "failed": failed,
        "warnings": warnings,
        "info": first_info,
    }


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


#----- Parts that belong to the same file (UI grouping)
@router.get("/{item_id}/parts")
async def list_import_parts(item_id: str):
    item = await failed_imports.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found.")
    targets = await _group_targets(item)
    return {
        "part_base": _item_part_base(item),
        "parts": [
            {
                "id": t.get("id"),
                "msg_id": t.get("msg_id"),
                "part_number": t.get("part_number"),
                "file_name": t.get("file_name"),
                "status": t.get("status"),
                "size_label": t.get("size_label"),
            }
            for t in targets
        ],
    }


#----- Map a failed file (and every sibling part) to the title the uploader picked
@router.post("/{item_id}/map")
async def map_import(item_id: str, body: MapBody, request: Request):
    item = await failed_imports.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found.")

    if not (item.get("file_name") or item.get("caption")):
        raise HTTPException(status_code=400, detail="This import has no filename or caption to parse.")

    selected = (body.selected_id or "").strip()
    if not selected:
        raise HTTPException(status_code=400, detail="selected_id is required.")

    quality = _normalize_quality(body.quality) or _normalize_quality(item.get("quality"))
    result = await _repair_parts(
        item,
        selected_id=_normalize_selected(selected, body.media_type),
        media_type=body.media_type,
        season=body.season,
        quality=quality,
        actor=_current_user(request),
    )

    if not result["ok"]:
        reason = (result["failed"][0]["reason"] if result["failed"] else "") or ""
        if reason == "no_quality":
            raise HTTPException(
                status_code=422,
                detail="This file has no quality. Add a quality (e.g. 1080p) and try again.",
            )
        raise HTTPException(status_code=422, detail="Could not resolve metadata for that title.")

    info = result["info"] or {}
    LOGGER.info(
        f"[imports] {item_id} mapped {result['parts_fixed']}/{result['parts_total']} part(s) "
        f"to {info.get('media_type')}/{info.get('tmdb_id')}"
    )

    return {
        "ok": True,
        "tmdb_id": info.get("tmdb_id"),
        "media_type": info.get("media_type"),
        "db_index": db.current_db_index,
        "parts_total": result["parts_total"],
        "parts_fixed": result["parts_fixed"],
        "parts_failed": result["parts_failed"],
        "warnings": result["warnings"],
    }


#----- Add/repair the quality of a failed import (all sibling parts), then retry
@router.post("/{item_id}/quality")
async def set_import_quality(item_id: str, body: QualityBody, request: Request):
    item = await failed_imports.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found.")

    quality = _normalize_quality(body.quality)
    if not quality:
        raise HTTPException(status_code=400, detail="A quality value is required (e.g. 1080p).")

    chat_id = int(item["chat_id"])
    targets = await _group_targets(item)

    # Persist the quality on every part and, optionally, in the Telegram caption.
    caption_updated = False
    for target in targets:
        await failed_imports.set_quality(target["id"], quality)
        if not body.edit_caption:
            continue
        base = (target.get("caption") or target.get("file_name") or "").strip()
        new_caption = _inject_quality(base, quality)
        if new_caption and new_caption != base:
            try:
                await edit_message(chat_id, int(target["msg_id"]), new_caption)
                caption_updated = True
            except Exception as e:
                LOGGER.warning(
                    f"[imports] caption edit failed for {chat_id}/{target['msg_id']}: {e}"
                )

    result = {
        "ok": False,
        "parts_total": len(targets),
        "parts_fixed": 0,
        "parts_failed": 0,
        "warnings": [],
        "info": None,
    }
    if body.retry:
        result = await _repair_parts(
            item,
            selected_id=None,
            media_type="movie",
            season=None,
            quality=quality,
            actor=_current_user(request),
        )
        if not result["ok"]:
            await failed_imports.set_status(item_id, "pending")
    else:
        for target in targets:
            await failed_imports.set_status(target["id"], "pending")

    info = result.get("info") or {}
    return {
        "ok": True,
        "quality": quality,
        "caption_updated": caption_updated,
        "mapped": bool(result.get("ok")),
        "parts_total": result.get("parts_total", len(targets)),
        "parts_fixed": result.get("parts_fixed", 0),
        "parts_failed": result.get("parts_failed", 0),
        "warnings": result.get("warnings", []),
        "tmdb_id": info.get("tmdb_id"),
        "media_type": info.get("media_type"),
        "db_index": db.current_db_index if result.get("ok") else None,
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
