from __future__ import annotations

import Backend
from fastapi import HTTPException

from Backend import db
from Backend.helper.metadata import (
    extract_default_id,
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    resolve_cover_url,
    search_any_candidates,
)
from Backend.logger import LOGGER



#----- Personal (hand-made) titles get a negative synthetic tmdb_id; real ones are positive
def _is_personal_media(tmdb_id) -> bool:
    try:
        return int(tmdb_id) < 0
    except (TypeError, ValueError):
        return False


#----- Normalize a media document into a compact session-picker result
def _session_result(doc: dict) -> dict:
    mt = doc.get("media_type") or doc.get("type") or "movie"
    mt = "tv" if str(mt).lower() in ("tv", "series") else "movie"
    imdb_id = doc.get("imdb_id") or ""
    tmdb_id = doc.get("tmdb_id")
    selected_id = imdb_id if str(imdb_id).startswith("tt") else (str(tmdb_id) if tmdb_id is not None else "")
    return {
        "tmdb_id": tmdb_id,
        "db_index": doc.get("db_index"),
        "media_type": mt,
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or "",
        "poster": resolve_cover_url(doc.get("poster") or ""),
        "imdb_id": imdb_id,
        "selected_id": selected_id,
        "is_personal": _is_personal_media(tmdb_id),
        "in_library": True,
    }


#----- Search the library, then IMDb/Cinemeta + TMDB, by title or an id/link
async def search_manual_session(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"results": []}

    results: list[dict] = []
    seen: set = set()

    def _add(doc: dict) -> None:
        entry = _session_result(doc)
        key = (entry["tmdb_id"], entry["db_index"], entry["media_type"])
        if entry["tmdb_id"] is None or key in seen:
            return
        seen.add(key)
        results.append(entry)

    default_id = extract_default_id(query)
    if default_id:
        try:
            if str(default_id).startswith("tt"):
                doc = await db.get_media_details(default_id)
                if doc:
                    _add(doc)
            else:
                for mt in ("movie", "tv"):
                    location = await db.find_media_doc(mt, int(default_id))
                    if location:
                        found, db_index = location
                        found["media_type"] = mt
                        found["db_index"] = db_index
                        _add(found)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] id lookup failed for '{query}': {e}")

    if not default_id:
        try:
            data = await db.search_documents(query, 1, 20)
            for doc in data.get("results", []):
                _add(doc)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] library search failed for '{query}': {e}")

    library_ids = {(e.get("imdb_id") or "", str(e.get("tmdb_id") or "")) for e in results}
    try:
        online = await search_any_candidates(query)
    except Exception as e:
        LOGGER.warning(f"[Manual Session] online search failed for '{query}': {e}")
        online = []

    for cand in online:
        if not cand.get("selected_id") or not cand.get("title"):
            continue
        imdb_id = cand.get("imdb_id") or ""
        tmdb_id = cand.get("tmdb_id")
        if (imdb_id, str(tmdb_id or "")) in library_ids:
            continue
        results.append({
            "tmdb_id": tmdb_id,
            "db_index": None,
            "media_type": "tv" if cand.get("media_type") == "tv" else "movie",
            "title": cand.get("title") or "",
            "year": cand.get("year") or "",
            "poster": resolve_cover_url(cand.get("poster") or ""),
            "imdb_id": imdb_id,
            "selected_id": str(cand.get("selected_id")),
            "source": cand.get("source"),
            "is_personal": False,
            "in_library": False,
        })

    return {"results": results}


#----- Current active manual upload session (or None)
async def get_manual_session() -> dict:
    return {"session": getattr(Backend, "MANUAL_SESSION", None)}


async def _set_online_manual_session(payload: dict, media_type: str, selected_id: str) -> dict:
    if not selected_id:
        raise HTTPException(status_code=400, detail="A library title or a selected id is required.")

    meta = await (
        fetch_selected_movie_metadata(selected_id) if media_type == "movie"
        else fetch_selected_tv_metadata(selected_id)
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")

    imdb_id = meta.get("imdb_id") or ""
    default_id = imdb_id if str(imdb_id).startswith("tt") else selected_id

    season = payload.get("season")
    if media_type == "tv" and season is not None and str(season).strip() != "":
        try:
            season = int(season)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Season must be a number.")
    else:
        season = None

    try:
        display_tmdb = int(meta.get("tmdb_id")) if meta.get("tmdb_id") is not None else 0
    except (TypeError, ValueError):
        display_tmdb = 0

    session = {
        "tmdb_id": display_tmdb,
        "db_index": None,
        "media_type": media_type,
        "title": meta.get("title") or "",
        "year": meta.get("release_year") or "",
        "is_personal": False,
        "kind": "real",
        "default_id": default_id,
        "season": season,
        "episode": None,
        "quality": None,
    }
    Backend.MANUAL_SESSION = session
    return {"status": "success", "session": session}


#----- Activate a manual upload session targeting an existing library title.
#----- Real (TMDB/IMDb) titles parse season/episode/quality from each file; personal
#----- (hand-made) titles need a season for TV since their files carry no metadata.
async def set_manual_session(payload: dict) -> dict:
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = _normalize_media_type(payload.get("media_type", "movie"))
    selected_id = str(payload.get("selected_id") or "").strip()
    in_library = payload.get("in_library", True) and tmdb_id is not None and db_index is not None

    if not in_library:
        return await _set_online_manual_session(payload, media_type, selected_id)

    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc:
        raise HTTPException(status_code=404, detail="That title was not found in your library.")

    is_personal = _is_personal_media(tmdb_id)
    session = {
        "tmdb_id": int(tmdb_id),
        "db_index": int(db_index),
        "media_type": media_type,
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or "",
        "is_personal": is_personal,
    }

    if is_personal:
        #----- Personal: files have no usable metadata, so season/episode come from here
        season = payload.get("season")
        episode = payload.get("episode")
        quality = str(payload.get("quality") or "").strip()

        if media_type == "tv":
            if season is None or str(season).strip() == "":
                raise HTTPException(status_code=400, detail="A season number is required for personal TV shows.")
            try:
                season = int(season)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Season must be a number.")
            if episode is not None and str(episode).strip() != "":
                try:
                    episode = int(episode)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="Episode must be a number.")
            else:
                episode = None
        else:
            season = None
            episode = None

        session.update({
            "kind": "personal",
            "default_id": None,
            "season": season,
            "episode": episode,
            "quality": quality or None,
        })
    else:
        #----- Real: force the title's own id and let metadata() parse from each file.
        #----- An optional season is only used as a fallback for files that carry an
        #----- episode but no season (e.g. absolute-numbered anime).
        imdb_id = doc.get("imdb_id") or ""
        default_id = imdb_id if str(imdb_id).startswith("tt") else str(int(tmdb_id))

        season = payload.get("season")
        if media_type == "tv" and season is not None and str(season).strip() != "":
            try:
                season = int(season)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Season must be a number.")
        else:
            season = None

        session.update({
            "kind": "real",
            "default_id": default_id,
            "season": season,
            "episode": None,
            "quality": None,
        })

    Backend.MANUAL_SESSION = session
    return {"status": "success", "session": session}


#----- Clear the active manual upload session
async def clear_manual_session() -> dict:
    Backend.MANUAL_SESSION = None
    return {"status": "success"}


