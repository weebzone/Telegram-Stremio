"""Persistence for files that failed automatic indexing.

Stored in the tracking DB (`failed_imports`). One document per Telegram
message, so re-forwarding the same file updates the existing row instead of
creating duplicates. Purely additive: nothing else in the schema changes.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from bson import ObjectId

from Backend.logger import LOGGER

COLLECTION = "failed_imports"


def _coll():
    from Backend import db

    return db.dbs["tracking"][COLLECTION]


async def ensure_indexes() -> None:
    try:
        coll = _coll()
        await coll.create_index([("status", 1), ("created_at", -1)])
        await coll.create_index([("chat_id", 1), ("msg_id", 1)], unique=True)
    except Exception as e:
        LOGGER.error(f"[imports] index creation failed: {e}")


def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out["id"] = str(out.pop("_id"))
    return out


async def record(
    *,
    chat_id,
    msg_id,
    file_name: str,
    caption: str,
    reason: str,
    size_bytes: int = 0,
    size_label: str = "",
    skip_msg_id=None,
    quality: str = "",
) -> None:
    now = time.time()
    set_fields = {
        "file_name": file_name or "",
        "caption": caption or "",
        "reason": reason,
        "size_bytes": int(size_bytes or 0),
        "size_label": size_label or "",
        "updated_at": now,
    }
    if skip_msg_id is not None:
        set_fields["skip_msg_id"] = skip_msg_id
    if quality:
        set_fields["quality"] = quality

    try:
        await _coll().update_one(
            {"chat_id": int(chat_id), "msg_id": int(msg_id)},
            {
                "$set": set_fields,
                "$setOnInsert": {
                    "status": "pending",
                    "created_at": now,
                    "fixed_by": None,
                    "media_tmdb_id": None,
                    "media_db_index": None,
                    "media_type": None,
                },
                "$inc": {"attempts": 1},
            },
            upsert=True,
        )
    except Exception as e:
        LOGGER.error(f"[imports] could not record {chat_id}/{msg_id}: {e}")


async def list_items(
    status: str = "pending",
    page: int = 1,
    page_size: int = 25,
    search: str = "",
) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if status and status != "all":
        query["status"] = status
    if search:
        query["$or"] = [
            {"file_name": {"$regex": search, "$options": "i"}},
            {"caption": {"$regex": search, "$options": "i"}},
        ]

    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    coll = _coll()
    total = await coll.count_documents(query)
    items = [
        _serialize(doc)
        async for doc in coll.find(query)
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_item(item_id: str) -> Optional[Dict[str, Any]]:
    try:
        oid = ObjectId(item_id)
    except Exception:
        return None
    doc = await _coll().find_one({"_id": oid})
    return _serialize(doc) if doc else None


async def set_quality(item_id: str, quality: str) -> bool:
    try:
        oid = ObjectId(item_id)
    except Exception:
        return False
    res = await _coll().update_one(
        {"_id": oid},
        {"$set": {"quality": quality, "updated_at": time.time()}},
    )
    return res.matched_count > 0


async def set_status(
    item_id: str,
    status: str,
    *,
    fixed_by: Optional[str] = None,
    tmdb_id=None,
    db_index=None,
    media_type: Optional[str] = None,
) -> bool:
    try:
        oid = ObjectId(item_id)
    except Exception:
        return False

    fields: Dict[str, Any] = {"status": status, "updated_at": time.time()}
    if fixed_by is not None:
        fields["fixed_by"] = fixed_by
    if tmdb_id is not None:
        fields["media_tmdb_id"] = tmdb_id
    if db_index is not None:
        fields["media_db_index"] = db_index
    if media_type is not None:
        fields["media_type"] = media_type

    res = await _coll().update_one({"_id": oid}, {"$set": fields})
    return res.matched_count > 0
