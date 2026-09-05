from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.media.crud import (
    add_subtitles_api,
    apply_media_rescan_api,
    delete_media_api,
    delete_movie_quality_api,
    delete_tv_episode_api,
    delete_tv_quality_api,
    delete_tv_season_api,
    get_media_visibility_api,
    list_manual_add_catalogs_api,
    list_media_api,
    list_subtitle_languages_api,
    list_subtitles_api,
    manual_add_media_api,
    remove_subtitle_api,
    resolve_manual_metadata_api,
    resolve_subtitle_api,
    resolve_telegram_api,
    search_media_rescan_api,
    set_media_visibility_api,
    speed_test_api,
    speed_test_stream_api,
    update_media_api,
)

router = APIRouter(tags=["media"])


@router.get("/api/media/list")
async def list_media(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100),
    custom: bool = Query(False),
    _: bool = Depends(require_auth),
):
    return await list_media_api(media_type, page, page_size, search, custom)


@router.delete("/api/media/delete")
async def delete_media(tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await delete_media_api(tmdb_id, db_index, media_type)


@router.put("/api/media/update")
async def update_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await update_media_api(request, tmdb_id, db_index, media_type)


@router.delete("/api/media/delete-quality")
async def delete_movie_quality(tmdb_id: int, db_index: int, id: str, _: bool = Depends(require_auth)):
    return await delete_movie_quality_api(tmdb_id, db_index, id)


@router.delete("/api/media/delete-tv-quality")
async def delete_tv_quality(
    tmdb_id: int, db_index: int, season: int, episode: int, id: str, _: bool = Depends(require_auth)
):
    return await delete_tv_quality_api(tmdb_id, db_index, season, episode, id)


@router.delete("/api/media/delete-tv-episode")
async def delete_tv_episode(
    tmdb_id: int, db_index: int, season: int, episode: int, _: bool = Depends(require_auth)
):
    return await delete_tv_episode_api(tmdb_id, db_index, season, episode)


@router.delete("/api/media/delete-tv-season")
async def delete_tv_season(tmdb_id: int, db_index: int, season: int, _: bool = Depends(require_auth)):
    return await delete_tv_season_api(tmdb_id, db_index, season)


@router.get("/api/system/speedtest")
async def speed_test(
    quality_id: str = Query(...),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(...),
    _: bool = Depends(require_auth),
):
    return await speed_test_api(quality_id, tmdb_id, db_index, media_type)


@router.get("/api/system/speedtest/stream")
async def speed_test_stream(
    quality_id: str = Query(...),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(...),
    _: bool = Depends(require_auth),
):
    return await speed_test_stream_api(quality_id, tmdb_id, db_index, media_type)


@router.get("/api/media/rescan/search")
async def search_media_rescan(
    media_type: str, query: str, year: int | None = None, _: bool = Depends(require_auth)
):
    return await search_media_rescan_api(media_type, query, year)


@router.post("/api/media/rescan/apply")
async def apply_media_rescan(
    request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)
):
    return await apply_media_rescan_api(request, tmdb_id, db_index, media_type)


@router.post("/api/media/manual-add")
async def manual_add_media(payload: dict, _: bool = Depends(require_auth)):
    return await manual_add_media_api(payload)


@router.get("/api/media/manual-add/catalogs")
async def manual_add_catalogs(_: bool = Depends(require_auth)):
    return await list_manual_add_catalogs_api()


@router.get("/api/media/manual-add/resolve-meta")
async def manual_add_resolve_meta(media_type: str, selected_id: str, _: bool = Depends(require_auth)):
    return await resolve_manual_metadata_api(media_type, selected_id)


@router.post("/api/media/resolve-telegram")
async def resolve_telegram(payload: dict, _: bool = Depends(require_auth)):
    return await resolve_telegram_api(payload)


@router.post("/api/media/subtitles/resolve")
async def resolve_subtitle(payload: dict, _: bool = Depends(require_auth)):
    return await resolve_subtitle_api(payload)


@router.get("/api/media/subtitles/languages")
async def list_subtitle_languages(_: bool = Depends(require_auth)):
    return list_subtitle_languages_api()


@router.get("/api/media/subtitles")
async def list_subtitles(media_type: str, tmdb_id: int, db_index: int, _: bool = Depends(require_auth)):
    return await list_subtitles_api(media_type, tmdb_id, db_index)


@router.post("/api/media/subtitles/add")
async def add_subtitles(payload: dict, _: bool = Depends(require_auth)):
    return await add_subtitles_api(payload)


@router.post("/api/media/subtitles/remove")
async def remove_subtitle(payload: dict, _: bool = Depends(require_auth)):
    return await remove_subtitle_api(payload)


@router.post("/api/custom-catalogs/media-visibility")
async def set_media_visibility(payload: dict, _: bool = Depends(require_auth)):
    return await set_media_visibility_api(payload)


@router.get("/api/custom-catalogs/media-visibility")
async def get_media_visibility(
    tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)
):
    return await get_media_visibility_api(tmdb_id, db_index, media_type)
