from __future__ import annotations

from fastapi import HTTPException

from Backend import db
from Backend.helper.content.auto_catalog import (
    get_auto_catalog_settings,
    get_auto_catalog_sync_status,
    start_auto_catalog_sync_background,
    update_auto_catalog_settings,
)
from Backend.logger import LOGGER

_VISIBILITY_MODES = ("public", "tokens", "owner")

#----- Custom catalog APIs
def _normalize_media_type(media_type: str) -> str:
    return "tv" if media_type in ["tv", "series"] else "movie"


async def list_custom_catalogs_api(
    tmdb_id: str | int | None = None,
    db_index: int | None = None,
    media_type: str | None = None,
):
    try:
        catalogs = await db.get_custom_catalogs()
        if tmdb_id is not None and db_index is not None and media_type:
            normalized_type = _normalize_media_type(media_type)
            for catalog in catalogs:
                catalog["contains_current"] = any(
                    int(item.get("tmdb_id", -1)) == int(tmdb_id)
                    and int(item.get("db_index", -1)) == int(db_index)
                    and item.get("media_type") == normalized_type
                    for item in catalog.get("items", []) or []
                )
        return {"catalogs": catalogs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



#----- Parse a (visibility, allowed_tokens) pair from a request payload
def _clean_visibility(payload: dict):
    visibility = payload.get("visibility")
    if visibility not in _VISIBILITY_MODES:
        visibility = None
    tokens = payload.get("allowed_tokens")
    tokens = [str(t).strip() for t in tokens if str(t).strip()] if isinstance(tokens, list) else []
    return visibility, tokens


async def create_custom_catalog_api(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Catalog name is required.")

    visibility, tokens = _clean_visibility(payload)
    catalog_id = await db.create_custom_catalog(name=name, visibility=visibility or "public", allowed_tokens=tokens)
    if not catalog_id:
        raise HTTPException(status_code=500, detail="Failed to create catalog.")

    catalog = await db.get_custom_catalog(catalog_id)
    return {"message": "Catalog created successfully.", "catalog": catalog}


async def update_custom_catalog_api(catalog_id: str, payload: dict):
    name = payload.get("name")
    visibility, tokens = _clean_visibility(payload)
    exclusive = payload.get("exclusive")
    exclusive = bool(exclusive) if exclusive is not None else None
    searchable = bool(payload.get("searchable"))
    result = await db.update_custom_catalog(
        catalog_id, name=name, visibility=visibility, allowed_tokens=tokens,
        exclusive=exclusive, searchable=searchable,
    )
    if not result:
        catalog = await db.get_custom_catalog(catalog_id)
        if not catalog:
            raise HTTPException(status_code=404, detail="Catalog not found.")
    return {"message": "Catalog updated successfully.", "catalog": await db.get_custom_catalog(catalog_id)}


#----- Set a title's visibility across every catalog it belongs to (used by media edit)

#----- Current effective visibility of a title (from the catalogs it belongs to)

async def delete_custom_catalog_api(catalog_id: str):
    result = await db.delete_custom_catalog(catalog_id)
    if not result:
        raise HTTPException(status_code=404, detail="Catalog not found.")
    return {"message": "Catalog deleted successfully."}


async def get_custom_catalog_items_api(
    catalog_id: str,
    media_type: str | None = None,
    page: int = 1,
    page_size: int = 24,
):
    try:
        data = await db.get_custom_catalog_items(catalog_id, media_type, page, page_size)
        if not data.get("catalog"):
            raise HTTPException(status_code=404, detail="Catalog not found.")
        _resolve_covers(data.get("items"))
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def search_catalog_media_api(
    query: str,
    media_type: str = "movie",
    page: int = 1,
    page_size: int = 12,
):
    query = (query or "").strip()
    if not query:
        return {"results": [], "total_count": 0}

    try:
        result = await db.search_documents(query, page, page_size)
        normalized_type = _normalize_media_type(media_type)
        filtered = [item for item in result.get("results", []) if item.get("media_type") == normalized_type]
        return {"results": filtered, "total_count": len(filtered)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def add_custom_catalog_item_api(catalog_id: str, payload: dict):
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = _normalize_media_type(payload.get("media_type", "movie"))

    if not tmdb_id or not db_index:
        raise HTTPException(status_code=400, detail="tmdb_id and db_index are required.")

    media = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not media:
        raise HTTPException(status_code=404, detail="Media not found.")

    catalog = await db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found.")

    added = await db.add_item_to_custom_catalog(catalog_id, int(tmdb_id), int(db_index), media_type)
    visibility_synced = None
    if added:
        #----- Adding to a hidden/restricted catalog adopts that visibility onto the title
        cat_vis = catalog.get("visibility")
        if cat_vis in ("owner", "tokens"):
            await db.set_media_visibility(
                int(tmdb_id), int(db_index), media_type, cat_vis, catalog.get("allowed_tokens") or []
            )
            visibility_synced = cat_vis
        if catalog.get("exclusive"):
            await db.mark_item_exclusive(catalog_id, int(tmdb_id), int(db_index), media_type, catalog.get("searchable", False))
    message = "Added to catalog." if added else "Already exists in this catalog."
    return {"message": message, "added": added, "visibility_synced": visibility_synced}


async def remove_custom_catalog_item_api(
    catalog_id: str,
    tmdb_id: str | int,
    db_index: int,
    media_type: str,
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    catalog = await db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found.")

    removed = await db.remove_item_from_custom_catalog(
        catalog_id, int(tmdb_id), int(db_index), _normalize_media_type(media_type)
    )
    if not removed:
        return {"message": "Item was not in this catalog.", "removed": False}
    if catalog.get("exclusive"):
        await db.clear_item_exclusive(int(tmdb_id), int(db_index), _normalize_media_type(media_type))
    return {"message": "Removed from catalog.", "removed": True}


async def auto_sync_custom_catalogs_api(force_refresh: bool = False):
    try:
        result = await start_auto_catalog_sync_background(db, force=True, force_refresh=force_refresh)
        return {"message": result.get("message", "Auto sync started."), "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def auto_catalog_sync_status_api():
    try:
        return {"status": await get_auto_catalog_sync_status(db)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def get_auto_catalog_settings_api():
    try:
        return {"settings": await get_auto_catalog_settings(db)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def update_auto_catalog_settings_api(payload: dict):
    try:
        enabled_keys = payload.get("enabled_keys", [])
        if not isinstance(enabled_keys, list):
            raise HTTPException(status_code=400, detail="enabled_keys must be a list.")
        settings = await update_auto_catalog_settings(db, enabled_keys)
        return {"message": "Auto catalog settings saved.", "settings": settings}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_DEFAULT_CATALOG_ENTRIES = [
    {"id": "latest_movies", "name": "Latest Movies", "group": "Default Movies", "type": "movie"},
    {"id": "top_movies", "name": "Popular Movies", "group": "Default Movies", "type": "movie"},
    {"id": "latest_series", "name": "Latest Series", "group": "Default TV", "type": "series"},
    {"id": "top_series", "name": "Popular Series", "group": "Default TV", "type": "series"},
]


async def get_catalog_order_api():
    try:
        catalogs = await db.get_custom_catalogs()
        entries = [dict(e) for e in _DEFAULT_CATALOG_ENTRIES]
        for c in catalogs:
            items = c.get("items") or []
            cid = f"custom_{c['_id']}"
            name = c.get("name") or "Catalog"
            group = "Auto" if c.get("auto") else "Custom"
            has_movie = any(i.get("media_type") == "movie" for i in items)
            has_series = any(i.get("media_type") == "tv" for i in items)
            if has_movie or not items:
                entries.append({"id": cid, "name": name, "group": group, "type": "movie"})
            if has_series:
                entries.append({"id": cid, "name": name, "group": group, "type": "series"})
        for e in entries:
            e["key"] = f"{e['id']}::{e['type']}"
        order = await db.get_catalog_order()
        rank = {k: i for i, k in enumerate(order)}
        entries.sort(key=lambda e: rank.get(e["key"], rank.get(e["id"], len(order) + 1)))
        return {"entries": entries, "order": order}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def update_catalog_order_api(payload: dict):
    order = payload.get("order")
    if not isinstance(order, list):
        raise HTTPException(status_code=400, detail="order must be a list.")
    await db.save_catalog_order(order)
    return {"ok": True, "message": "Catalog order saved."}












