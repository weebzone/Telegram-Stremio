from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.catalogs.catalogs import (
    add_custom_catalog_item_api,
    auto_catalog_sync_status_api,
    auto_sync_custom_catalogs_api,
    create_custom_catalog_api,
    delete_custom_catalog_api,
    get_auto_catalog_settings_api,
    get_catalog_order_api,
    get_custom_catalog_items_api,
    list_custom_catalogs_api,
    remove_custom_catalog_item_api,
    search_catalog_media_api,
    update_auto_catalog_settings_api,
    update_catalog_order_api,
    update_custom_catalog_api,
)

router = APIRouter(tags=["catalogs"])


@router.get("/api/custom-catalogs")
async def list_custom_catalogs(
    tmdb_id: int | None = None,
    db_index: int | None = None,
    media_type: str | None = None,
    _: bool = Depends(require_auth),
):
    return await list_custom_catalogs_api(tmdb_id, db_index, media_type)


@router.post("/api/custom-catalogs")
async def create_custom_catalog(payload: dict, _: bool = Depends(require_auth)):
    return await create_custom_catalog_api(payload)


@router.put("/api/custom-catalogs/{catalog_id}")
async def update_custom_catalog(catalog_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await update_custom_catalog_api(catalog_id, payload)


@router.delete("/api/custom-catalogs/{catalog_id}")
async def delete_custom_catalog(catalog_id: str, _: bool = Depends(require_auth)):
    return await delete_custom_catalog_api(catalog_id)


@router.get("/api/custom-catalogs/search-media")
async def search_catalog_media(
    query: str,
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=50),
    _: bool = Depends(require_auth),
):
    return await search_catalog_media_api(query, media_type, page, page_size)


@router.post("/api/custom-catalogs/auto-sync")
async def auto_sync_custom_catalogs(
    force_refresh: bool = Query(False),
    _: bool = Depends(require_auth),
):
    return await auto_sync_custom_catalogs_api(force_refresh)


@router.get("/api/custom-catalogs/auto-sync/status")
async def auto_catalog_sync_status(_: bool = Depends(require_auth)):
    return await auto_catalog_sync_status_api()


@router.get("/api/custom-catalogs/auto-sync/settings")
async def get_auto_catalog_settings_route(_: bool = Depends(require_auth)):
    return await get_auto_catalog_settings_api()


@router.put("/api/custom-catalogs/auto-sync/settings")
async def update_auto_catalog_settings_route(payload: dict, _: bool = Depends(require_auth)):
    return await update_auto_catalog_settings_api(payload)


@router.get("/api/custom-catalogs-order")
async def get_catalog_order(_: bool = Depends(require_auth)):
    return await get_catalog_order_api()


@router.put("/api/custom-catalogs-order")
async def update_catalog_order(payload: dict, _: bool = Depends(require_auth)):
    return await update_catalog_order_api(payload)


@router.get("/api/custom-catalogs/{catalog_id}/items")
async def get_custom_catalog_items(
    catalog_id: str,
    media_type: str | None = Query(None, regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    _: bool = Depends(require_auth),
):
    return await get_custom_catalog_items_api(catalog_id, media_type, page, page_size)


@router.post("/api/custom-catalogs/{catalog_id}/items")
async def add_custom_catalog_item(catalog_id: str, payload: dict, _: bool = Depends(require_auth)):
    return await add_custom_catalog_item_api(catalog_id, payload)


@router.delete("/api/custom-catalogs/{catalog_id}/items")
async def remove_custom_catalog_item(
    catalog_id: str,
    tmdb_id: int,
    db_index: int,
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    _: bool = Depends(require_auth),
):
    return await remove_custom_catalog_item_api(catalog_id, tmdb_id, db_index, media_type)
