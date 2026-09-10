"""
api/media.py — library media CRUD and quality / episode management.

List, update, delete media; remove qualities, episodes, seasons; rescan
metadata; manual-add from Telegram; visibility and catalog search.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Query, Request

from Backend import db
from Backend.helper.media_extras.auto_catalog import (
    start_single_media_catalog_sync,
)
from Backend.helper.security.encrypt import encode_string
from Backend.helper.tools.manual_add import resolve_telegram_message, stamp_caption_by_ref
from Backend.helper.metadata import (
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    resolve_cover_url,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.telegram.split_files import strip_part_suffix
from Backend.helper.ops.announcer import delete_announcement_async

from Backend.fastapi.routes.api._helpers import (
    _VISIBILITY_MODES,
    _require_tmdb_id,
    _resolve_covers,
    _scan_client,
    _fill_placeholder_metadata,
    _clean_visibility,
    _normalize_media_type,
    _metadata_base,
)

async def list_media_api(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100),
    custom: bool = Query(False)
):
    try:
        key = "movies" if media_type == "movie" else "tv_shows"
        extra_filter = {"tmdb_id": {"$lt": 0}} if custom else None
        if search:
            result = await db.search_documents(search, page, page_size)
            filtered_results = [
                item for item in result['results']
                if item.get('media_type') == media_type and (not custom or int(item.get('tmdb_id') or 0) < 0)
            ]
            total_filtered = len(filtered_results)
            start_index = (page - 1) * page_size
            resp = {
                "total_count": total_filtered,
                "current_page": page,
                "total_pages": (total_filtered + page_size - 1) // page_size,
                key: filtered_results[start_index:start_index + page_size],
            }
        elif media_type == "movie":
            resp = await db.sort_movies([], page, page_size, extra_filter=extra_filter)
        else:
            resp = await db.sort_tv_shows([], page, page_size, extra_filter=extra_filter)
        _resolve_covers(resp.get(key))
        return resp
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_media_api(
    tmdb_id: str | int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        media_type_formatted = "Movie" if media_type == "movie" else "Series"
        result = await db.delete_document(media_type_formatted, tmdb_id, db_index)
        if result:
            delete_announcement_async(media_type, tmdb_id)
            return {"message": "Media deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def update_media_api(
    request: Request,
    tmdb_id: str | int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        update_data = await request.json()
        if 'rating' in update_data and update_data['rating']:
            try:
                update_data['rating'] = float(update_data['rating'])
            except (ValueError, TypeError):
                update_data['rating'] = 0.0

        if 'release_year' in update_data and update_data['release_year']:
            try:
                update_data['release_year'] = int(update_data['release_year'])
            except (ValueError, TypeError):
                pass
        if 'genres' in update_data:
            if isinstance(update_data['genres'], str):
                update_data['genres'] = [g.strip() for g in update_data['genres'].split(',') if g.strip()]
            elif not isinstance(update_data['genres'], list):
                update_data['genres'] = []

        if 'languages' in update_data:
            if isinstance(update_data['languages'], str):
                update_data['languages'] = [l.strip() for l in update_data['languages'].split(',') if l.strip()]
            elif not isinstance(update_data['languages'], list):
                update_data['languages'] = []
        if media_type == "movie":
            if 'runtime' in update_data and update_data['runtime']:
                try:
                    update_data['runtime'] = int(update_data['runtime'])
                except (ValueError, TypeError):
                    pass
        elif media_type == "tv":
            if 'total_seasons' in update_data and update_data['total_seasons']:
                try:
                    update_data['total_seasons'] = int(update_data['total_seasons'])
                except (ValueError, TypeError):
                    pass

            if 'total_episodes' in update_data and update_data['total_episodes']:
                try:
                    update_data['total_episodes'] = int(update_data['total_episodes'])
                except (ValueError, TypeError):
                    pass
        update_data = {k: v for k, v in update_data.items() if v != ""}
        if "title" in update_data:
            update_data["title_english"] = update_data["title"]
        result = await db.update_document(media_type, tmdb_id, db_index, update_data)
        if result:
            return {"message": "Media updated successfully"}
        else:
            raise HTTPException(status_code=404, detail="Media not found or no changes made")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def get_media_details_api(
    tmdb_id: str | int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        result = await db.get_document(media_type, tmdb_id, db_index)
        if result:
            return result
        else:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_movie_quality_api(tmdb_id: str | int, db_index: int, id: str):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        result = await db.delete_movie_quality(tmdb_id, db_index, id)
        if result:
            return {"message": "Quality deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Quality not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_tv_quality_api(
    tmdb_id: str | int, db_index: int, season: int, episode: int, id: str
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        result = await db.delete_tv_quality(tmdb_id, db_index, season, episode, id)
        if result:
            return {"message": "deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Quality not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_tv_episode_api(
    tmdb_id: str | int, db_index: int, season: int, episode: int
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        result = await db.delete_tv_episode(tmdb_id, db_index, season, episode)
        if result:
            return {"message": "Episode deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Episode not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_tv_season_api(tmdb_id: str | int, db_index: int, season: int):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        result = await db.delete_tv_season(tmdb_id, db_index, season)
        if result:
            return {"message": "Season deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Season not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def search_media_rescan_api(media_type: str, query: str, year: int | None = None):
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required.")

    if media_type == "movie":
        results = await search_movie_candidates(query=query, year=year)
    elif media_type == "tv":
        results = await search_tv_candidates(query=query)
    else:
        raise HTTPException(status_code=400, detail="Invalid media_type.")

    return {"results": results}

async def apply_media_rescan_api(request: Request, tmdb_id: str | int, db_index: int, media_type: str):
    tmdb_id = _require_tmdb_id(tmdb_id)
    body = await request.json()
    selected_id = str(body.get("selected_id") or "").strip()

    if not selected_id:
        raise HTTPException(status_code=400, detail="selected_id is required.")

    current_doc = await db.get_document(media_type, tmdb_id, db_index)
    if not current_doc:
        raise HTTPException(status_code=404, detail="Media not found.")

    if media_type == "movie":
        metadata = await fetch_selected_movie_metadata(selected_id)
    elif media_type == "tv":
        metadata = await fetch_selected_tv_metadata(selected_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid media_type.")

    if not metadata:
        raise HTTPException(status_code=404, detail="Unable to fetch metadata for selected item.")

    updated_doc = await db.replace_media_metadata(
        media_type=media_type,
        tmdb_id=tmdb_id,
        db_index=db_index,
        metadata=metadata,
    )

    if not updated_doc:
        raise HTTPException(status_code=500, detail="Failed to replace media metadata.")

    return {
        "success": True,
        "message": "Metadata rescanned successfully.",
        "redirect_tmdb_id": updated_doc.get("tmdb_id"),
        "db_index": updated_doc.get("db_index", db_index),
        "media_type": media_type,
        "data": updated_doc,
}

async def manual_add_media_api(payload: dict) -> dict:
    media_type = payload.get("media_type")
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'.")

    stream = payload.get("stream") or {}
    quality = str(stream.get("quality") or "").strip()
    if not quality:
        raise HTTPException(status_code=400, detail="A quality label (e.g. 1080p) is required.")

    part_sources = stream.get("parts")
    if not isinstance(part_sources, list) or not part_sources:
        part_sources = [{"url": stream.get("url"), "chat_id": stream.get("chat_id"), "msg_id": stream.get("msg_id")}]
    part_sources = [p for p in part_sources if p and (p.get("url") or (p.get("chat_id") and p.get("msg_id")))]
    if not part_sources:
        raise HTTPException(status_code=400, detail="Provide at least one Telegram message link.")

    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    resolved_parts = []
    for src in part_sources:
        try:
            resolved_parts.append(await resolve_telegram_message(
                client, url=src.get("url"), chat_id=src.get("chat_id"), msg_id=src.get("msg_id"),
            ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")

    primary = resolved_parts[0]
    is_split = len(resolved_parts) > 1
    raw_name = (stream.get("name") or primary["name"]).strip()
    name = strip_part_suffix(raw_name) if is_split else raw_name

    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    selected_id = str(payload.get("selected_id") or "").strip()

    base = None
    if tmdb_id and db_index:
        doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
        if doc:
            base = _metadata_base(doc, from_doc=True)
    if base is None and selected_id:
        selection = await (
            fetch_selected_movie_metadata(selected_id) if media_type == "movie"
            else fetch_selected_tv_metadata(selected_id)
        )
        if not selection:
            raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")
        base = _metadata_base(selection, from_doc=True)
    if base is None:
        base = _metadata_base(payload.get("manual_metadata") or {})
        if not base["title"]:
            raise HTTPException(status_code=400, detail="A title is required for manual entry.")
        if not base["year"]:
            base["year"] = int(primary.get("upload_year") or 0)

    if not base.get("tmdb_id"):
        base["tmdb_id"] = -(secrets.randbelow(2_000_000_000) + 1)
    if not base.get("imdb_id"):
        base["imdb_id"] = f"tg{abs(int(base['tmdb_id']))}"
    _fill_placeholder_metadata(base)

    thumb_url = ""
    if primary.get("has_thumb"):
        thumb_enc = await encode_string({"chat_id": int(primary["chat_id"]), "msg_id": int(primary["msg_id"])})
        thumb_url = f"/thumb/{thumb_enc}"

    group_key = f"manual:{primary['chat_id']}:{quality}:{secrets.token_hex(6)}" if is_split else None

    tv_extra = {}
    if media_type == "tv":
        try:
            season_number = int(payload.get("season_number"))
            episode_number = int(payload.get("episode_number"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Season and episode numbers are required for TV.")
        tv_extra = {
            "season_number": season_number,
            "episode_number": episode_number,
            "episode_title": (payload.get("episode_title") or "").strip() or f"S{season_number:02d}E{episode_number:02d}",
            "episode_backdrop": payload.get("episode_backdrop") or thumb_url or base.get("backdrop") or "",
            "episode_overview": payload.get("episode_overview") or "",
            "episode_released": payload.get("episode_released") or "",
        }

    for index, part in enumerate(resolved_parts, start=1):
        p_channel = int(part["chat_id"])
        p_msg = int(part["msg_id"])
        encoded = await encode_string({"chat_id": p_channel, "msg_id": p_msg})
        metadata_info = dict(base)
        metadata_info.update({
            "media_type": media_type,
            "quality": quality,
            "encoded_string": encoded,
            "group_key": group_key,
            "part_number": index if is_split else None,
            "is_anime": False,
        })
        metadata_info.update(tv_extra)
        updated_id = await db.insert_media(
            metadata_info, channel=p_channel, msg_id=p_msg,
            size=part["size"], name=name, raw_size=int(part.get("raw_size") or 0),
        )
        if not updated_id:
            raise HTTPException(status_code=500, detail="Failed to add media (validation error).")
        await stamp_caption_by_ref(client, p_channel, p_msg, metadata_info)

    result_tmdb_id = base["tmdb_id"]
    location = await db.find_media_doc(media_type, result_tmdb_id)
    result_db_index = location[1] if location else db.current_db_index

    catalog_ids = payload.get("catalog_ids") or []
    catalogs_added = []
    if location:
        for cat_id in catalog_ids:
            try:
                cat_id = str(cat_id).strip()
                if not cat_id:
                    continue
                added = await db.add_item_to_custom_catalog(cat_id, int(result_tmdb_id), int(result_db_index), media_type)
                if added:
                    catalog = await db.get_custom_catalog(cat_id)
                    if catalog:
                        catalogs_added.append(catalog.get("name", cat_id))
                        cat_vis = catalog.get("visibility")
                        if cat_vis in ("owner", "tokens"):
                            await db.set_media_visibility(
                                int(result_tmdb_id), int(result_db_index), media_type,
                                cat_vis, catalog.get("allowed_tokens") or []
                            )
                        if catalog.get("exclusive"):
                            await db.mark_item_exclusive(
                                cat_id, int(result_tmdb_id), int(result_db_index),
                                media_type, catalog.get("searchable", False)
                            )
            except Exception:
                pass

    if result_tmdb_id and result_tmdb_id > 0:
        try:
            start_single_media_catalog_sync(db, tmdb_id=result_tmdb_id, media_type=media_type)
        except Exception:
            pass

    message = f"Split stream added ({len(resolved_parts)} parts)." if is_split else "Stream added successfully."
    if catalogs_added:
        message += f" Added to: {', '.join(catalogs_added)}."
    return {
        "status": "success",
        "message": message,
        "tmdb_id": result_tmdb_id,
        "db_index": result_db_index,
        "media_type": media_type,
    }

async def list_manual_add_catalogs_api():
    try:
        catalogs = await db.get_custom_catalogs()
        filtered = [c for c in catalogs if not c.get("auto")]
        filtered.sort(key=lambda c: (0 if c.get("exclusive") else 1, (c.get("name") or "").lower()))
        return {"catalogs": [
            {"_id": c["_id"], "name": c["name"], "exclusive": bool(c.get("exclusive")),
             "visibility": c.get("visibility", "public")}
            for c in filtered
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

_VISIBILITY_MODES = ("public", "tokens", "owner")

async def resolve_manual_metadata_api(media_type: str, selected_id: str) -> dict:
    selected_id = str(selected_id or "").strip()
    if not selected_id:
        raise HTTPException(status_code=400, detail="selected_id is required.")
    mt = _normalize_media_type(media_type)
    data = await (
        fetch_selected_movie_metadata(selected_id) if mt == "movie"
        else fetch_selected_tv_metadata(selected_id)
    )
    if not data:
        raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")
    if data.get("poster"):
        data["poster"] = resolve_cover_url(data["poster"])
    if data.get("backdrop"):
        data["backdrop"] = resolve_cover_url(data["backdrop"])
    return {"metadata": data}

async def set_media_visibility_api(payload: dict):
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = payload.get("media_type")
    if not tmdb_id or not db_index or media_type not in ("movie", "tv", "series"):
        raise HTTPException(status_code=400, detail="tmdb_id, db_index and media_type are required.")

    visibility, tokens = _clean_visibility(payload)
    if not visibility:
        raise HTTPException(status_code=400, detail="A valid visibility is required.")

    count = await db.set_media_visibility(
        int(tmdb_id), int(db_index), _normalize_media_type(media_type), visibility, tokens
    )
    return {
        "status": "success",
        "updated_catalogs": count,
        "message": "Visibility updated — applies to default catalogs and every catalog this title is in.",
    }

async def get_media_visibility_api(tmdb_id: str | int, db_index: int, media_type: str):
    tmdb_id = _require_tmdb_id(tmdb_id)
    data = await db.get_media_visibility(int(tmdb_id), int(db_index), _normalize_media_type(media_type))
    return {"visibility": data or {}}

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

async def resolve_telegram_api(payload: dict) -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    try:
        data = await resolve_telegram_message(
            client,
            url=payload.get("url"),
            chat_id=payload.get("chat_id"),
            msg_id=payload.get("msg_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")
    return {"status": "success", "data": data}
