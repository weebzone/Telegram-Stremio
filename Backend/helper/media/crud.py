from __future__ import annotations

import asyncio
import json
import random
from time import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from Backend import StartTime, __version__, db
from Backend.helper.streaming.custom_dl import ByteStreamer, run_speed_test, _speed_test_single_client
from Backend.helper.core.encrypt import decode_string
from Backend.helper.content.manual_add import resolve_telegram_message, stamp_caption_by_ref
from Backend.helper.metadata import (
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    gradient_cover_path,
    resolve_cover_url,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.content.subtitles import (
    list_languages,
    list_title_subtitles,
    manual_ingest_subtitle,
    remove_subtitle,
    resolve_subtitle_message,
)
from Backend.helper.telegram.pyro import get_readable_time, get_scan_client
from Backend.helper.telegram.announcer import delete_announcement_async
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, multi_clients

def _coerce_tmdb_id(value):
    """Accept int or string; treat 'null'/''/None as missing."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "undefined"):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _require_tmdb_id(value) -> int:
    tid = _coerce_tmdb_id(value)
    if tid is None:
        raise HTTPException(status_code=400, detail="tmdb_id is required and must be an integer")
    return tid



def _resolve_covers(items) -> None:
    for item in items or []:
        for key in ("poster", "backdrop"):
            if item.get(key):
                item[key] = resolve_cover_url(item[key])


#----- Media management
async def list_media_api(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100),
    custom: bool = Query(False)
):
    try:
        key = "movies" if media_type == "movie" else "tv_shows"
        #----- Custom (manually added) titles carry a negative synthetic tmdb_id
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
            # Remove matching announcement post from the announcement channel
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



#----- Speed test
#----- Decode a quality_id into (chat_id, msg_id); split files use the first part
async def _resolve_speed_test_target(quality_id: str):
    decoded = await decode_string(quality_id)
    target = decoded["parts"][0] if decoded.get("parts") else decoded
    msg_id = target.get("msg_id")
    raw_cid = target.get("chat_id")
    if not msg_id or not raw_cid:
        return None, None, decoded
    return int(f"-100{raw_cid}"), int(msg_id), decoded


#----- Run a parallel download speed test across all connected clients
async def speed_test_api(
    quality_id: str = Query(..., description="Encoded quality ID from DB"),
    tmdb_id: str | int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(..., regex="^(movie|tv)$"),
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        chat_id, msg_id, decoded = await _resolve_speed_test_target(quality_id)
        if not chat_id or not msg_id:
            raise HTTPException(
                status_code=422,
                detail=f"Decoded quality data is missing msg_id or chat_id. Decoded: {decoded}"
            )

        results = await run_speed_test(chat_id, msg_id)
        return {"results": results, "total_clients_tested": len(results)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- SSE speed test streaming per-client results as they finish
async def speed_test_stream_api(
    quality_id: str,
    tmdb_id: str | int,
    db_index: int,
    media_type: str,
):

    tmdb_id = _require_tmdb_id(tmdb_id)
    async def event_generator():
        try:
            chat_id, msg_id, decoded = await _resolve_speed_test_target(quality_id)
            if not chat_id or not msg_id:
                payload = json.dumps({"type": "error", "message": f"Cannot decode quality_id. Got: {decoded}"})
                yield f"data: {payload}\n\n"
                return
        except Exception as exc:
            payload = json.dumps({"type": "error", "message": str(exc)})
            yield f"data: {payload}\n\n"
            return

        total = len(multi_clients)
        if total == 0:
            payload = json.dumps({"type": "error", "message": "No bot clients connected"})
            yield f"data: {payload}\n\n"
            return

        #----- Resolve the FileId to report the target DC
        target_dc = "?"
        try:
            primary_client = multi_clients.get(0) or next(iter(multi_clients.values()))
            streamer = ByteStreamer(primary_client)
            file_id = await streamer.get_file_properties(chat_id, int(msg_id))
            target_dc = file_id.dc_id
        except Exception:
            pass

        #----- Initial start event so the frontend can build its table
        yield f"data: {json.dumps({'type': 'start', 'total': total, 'target_dc': target_dc})}\n\n"

        #----- Run all clients in parallel, feeding results into a queue
        queue: asyncio.Queue = asyncio.Queue()

        async def run_one(client, idx):
            async def on_progress(prog_data):
                await queue.put({"type": "progress", "data": prog_data})

            result = await _speed_test_single_client(
                client, idx, chat_id, int(msg_id), progress_callback=on_progress
            )
            await queue.put({"type": "result", "data": result})

        tasks = [
            asyncio.create_task(run_one(client, idx))
            for idx, client in multi_clients.items()
        ]

        completed = 0
        while completed < total:
            msg = await queue.get()

            if msg["type"] == "progress":
                payload = json.dumps(msg)
                yield f"data: {payload}\n\n"

            elif msg["type"] == "result":
                completed += 1
                payload = json.dumps({
                    "type": "result",
                    "data": msg["data"],
                    "completed": completed,
                    "total": total,
                })
                yield f"data: {payload}\n\n"

        await asyncio.gather(*tasks, return_exceptions=True)
        yield f"data: {json.dumps({'type': 'done', 'total': total})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


#----- Rescan: search TMDB candidates for a title
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


#----- Manual add: fetch full metadata for a selected TMDB/IMDB title to autofill the form
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


#----- Manual add: resolve a Telegram post link into a streamable file
async def resolve_telegram_api(payload: dict) -> dict:
    client = get_scan_client()
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


async def resolve_subtitle_api(payload: dict) -> dict:
    client = get_scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    try:
        data = await resolve_subtitle_message(
            client, url=payload.get("url"),
            chat_id=payload.get("chat_id"), msg_id=payload.get("msg_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")
    return {"status": "success", "data": data}


async def _resolve_imdb_id(media_type: str, tmdb_id, db_index) -> str:
    tmdb_id = _coerce_tmdb_id(tmdb_id)
    if not (tmdb_id and db_index):
        raise HTTPException(status_code=400, detail="tmdb_id and db_index are required.")
    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc or not doc.get("imdb_id"):
        raise HTTPException(status_code=404, detail="Title not found.")
    return doc["imdb_id"]


def list_subtitle_languages_api() -> dict:
    return {"status": "success", "languages": list_languages()}


async def list_subtitles_api(media_type: str, tmdb_id, db_index) -> dict:
    tmdb_id = _require_tmdb_id(tmdb_id)
    mt = "tv" if media_type in ("tv", "series") else "movie"
    imdb_id = await _resolve_imdb_id(mt, tmdb_id, db_index)
    return {"status": "success", "subtitles": await list_title_subtitles(imdb_id)}


async def add_subtitles_api(payload: dict) -> dict:
    media_type = "tv" if payload.get("media_type") in ("tv", "series") else "movie"
    imdb_id = await _resolve_imdb_id(media_type, payload.get("tmdb_id"), payload.get("db_index"))
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="Provide at least one subtitle to add.")

    client = get_scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    added, errors = [], []
    for item in items:
        try:
            season = item.get("season") if media_type == "tv" else None
            episode = item.get("episode") if media_type == "tv" else None
            if media_type == "tv" and (not season or not episode):
                raise ValueError("Season and episode are required for series subtitles.")
            resolved = await resolve_subtitle_message(
                client, url=item.get("url"),
                chat_id=item.get("chat_id"), msg_id=item.get("msg_id"),
            )
            doc = await manual_ingest_subtitle(
                imdb_id, media_type, season, episode,
                item.get("lang_code") or resolved["lang_code"],
                resolved["chat_id"], resolved["msg_id"], resolved["name"],
            )
            added.append({
                "name": doc["name"], "lang_label": doc["lang_label"],
                "season": doc["season"], "episode": doc["episode"],
            })
        except ValueError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"Could not add subtitle: {exc}")

    if not added and errors:
        raise HTTPException(status_code=400, detail=" ".join(errors))
    message = f"Added {len(added)} subtitle(s)."
    if errors:
        message += f" {len(errors)} failed: {' '.join(errors)}"
    return {"status": "success", "message": message, "added": added, "errors": errors}


async def remove_subtitle_api(payload: dict) -> dict:
    chat_id = payload.get("chat_id")
    msg_id = payload.get("msg_id")
    if chat_id in (None, "") or msg_id in (None, ""):
        raise HTTPException(status_code=400, detail="chat_id and msg_id are required.")
    if not await remove_subtitle(chat_id, msg_id):
        raise HTTPException(status_code=404, detail="Subtitle not found.")
    return {"status": "success", "message": "Subtitle removed."}


#----- Build a metadata base (title-level fields) from various sources
def _metadata_base(source: dict, from_doc: bool = False) -> dict:
    genres = source.get("genres")
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",") if g.strip()]
    year = source.get("release_year") if from_doc else source.get("year")
    rate = source.get("rating") if from_doc else source.get("rate")
    return {
        "tmdb_id": source.get("tmdb_id"),
        "imdb_id": source.get("imdb_id") or None,
        "title": (source.get("title") or "").strip(),
        "year": int(year) if str(year or "").strip().lstrip("-").isdigit() else 0,
        "rate": float(rate) if str(rate or "").replace(".", "", 1).isdigit() else 0,
        "description": source.get("description") or "",
        "poster": source.get("poster") or "",
        "backdrop": source.get("backdrop") or "",
        "logo": source.get("logo") or "",
        "genres": genres or [],
        "cast": source.get("cast") or [],
        "runtime": str(source.get("runtime") or ""),
        "original_language": source.get("original_language"),
        "origin_country": source.get("origin_country") or [],
    }


_PLACEHOLDER_GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy",
                       "Thriller", "Mystery", "Sci-Fi", "Romance", "Family"]
_PLACEHOLDER_DESCRIPTIONS = [
    "A gripping story full of unexpected twists and turns.",
    "An unforgettable journey that keeps you on the edge of your seat.",
    "A captivating tale of drama, courage and emotion.",
    "An entertaining experience packed with memorable moments.",
    "A thrilling adventure blending heart, action and wonder.",
]


#----- Fill empty optional metadata with random values and a gradient cover path
def _fill_placeholder_metadata(meta: dict) -> None:
    title = meta.get("title") or "Media"
    if not meta.get("poster"):
        meta["poster"] = gradient_cover_path(title, portrait=True)
    if not meta.get("backdrop"):
        meta["backdrop"] = gradient_cover_path(title)
    if not meta.get("genres"):
        meta["genres"] = random.sample(_PLACEHOLDER_GENRES, random.randint(1, 3))
    if not meta.get("rate"):
        meta["rate"] = round(random.uniform(6.0, 8.9), 1)
    if not meta.get("description"):
        meta["description"] = random.choice(_PLACEHOLDER_DESCRIPTIONS)


#----- Manual add: create/append a movie, tv show, season, episode or stream by hand
async def manual_add_media_api(payload: dict) -> dict:
    media_type = payload.get("media_type")
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'.")

    stream = payload.get("stream") or {}
    quality = str(stream.get("quality") or "").strip()
    if not quality:
        raise HTTPException(status_code=400, detail="A quality label (e.g. 1080p) is required.")

    #----- One source = single file, multiple sources = split file parts (in order)
    part_sources = stream.get("parts")
    if not isinstance(part_sources, list) or not part_sources:
        part_sources = [{"url": stream.get("url"), "chat_id": stream.get("chat_id"), "msg_id": stream.get("msg_id")}]
    part_sources = [p for p in part_sources if p and (p.get("url") or (p.get("chat_id") and p.get("msg_id")))]
    if not part_sources:
        raise HTTPException(status_code=400, detail="Provide at least one Telegram message link.")

    client = get_scan_client()
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

    #----- Resolve the title-level metadata: existing doc, TMDB/IMDb pick, or manual entry
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

    #----- Brand-new hand-made titles get a negative synthetic id (never collides with TMDB)
    if not base.get("tmdb_id"):
        base["tmdb_id"] = -(secrets.randbelow(2_000_000_000) + 1)
    #----- A synthetic "tg" imdb id is required so Stremio can request meta/streams
    if not base.get("imdb_id"):
        base["imdb_id"] = f"tg{abs(int(base['tmdb_id']))}"
    _fill_placeholder_metadata(base)

    #----- Store the file thumbnail as a base-relative path so it survives base_url changes
    thumb_url = ""
    if primary.get("has_thumb"):
        thumb_enc = await encode_string({"chat_id": int(primary["chat_id"]), "msg_id": int(primary["msg_id"])})
        thumb_url = f"/thumb/{thumb_enc}"

    #----- Split parts share one quality entry via a common group key
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

    #----- Assign to selected custom catalogs before triggering auto sync, so any
    #----- exclusivity is stamped on the doc first and auto sync correctly skips it.
    #----- Guarded on `location` so we never add a reference to a non-existent doc.
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


#----- Current effective visibility of a title (from the catalogs it belongs to)
async def get_media_visibility_api(tmdb_id: str | int, db_index: int, media_type: str):
    tmdb_id = _require_tmdb_id(tmdb_id)
    data = await db.get_media_visibility(int(tmdb_id), int(db_index), _normalize_media_type(media_type))
    return {"visibility": data or {}}


