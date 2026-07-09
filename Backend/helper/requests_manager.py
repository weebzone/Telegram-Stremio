import hashlib
import re
from datetime import datetime

from bson import ObjectId
from pymongo import ReturnDocument

from Backend import db
from Backend.helper.imdb import extract_first_year, get_detail as cinemeta_detail, search_title_multi
from Backend.helper.metadata import (
    extract_default_id,
    format_tmdb_image,
    get_tmdb_client,
    tmdb_api_key,
)
from Backend.logger import LOGGER

STATUSES = ("pending", "uploaded", "denied", "banned")
_IMDB_RE = re.compile(r"(tt\d{7,10})")


def _norm_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _coll():
    return db.dbs["tracking"]["requests"]


def _norm_type(media_type: str) -> str:
    return "tv" if media_type in ("tv", "series") else "movie"


def _hash_ip(ip: str) -> str:
    return hashlib.sha256((ip or "unknown").encode()).hexdigest()[:16]


def _poster(path: str) -> str:
    return format_tmdb_image(path, "w342") if path else ""


def _movie_entry(m) -> dict:
    date = getattr(m, "release_date", None)
    return {
        "media_type": "movie",
        "tmdb_id": getattr(m, "id", None),
        "title": getattr(m, "title", None) or getattr(m, "original_title", None) or "Untitled",
        "year": getattr(date, "year", None) if date else None,
        "poster": _poster(getattr(m, "poster_path", None)),
        "overview": (getattr(m, "overview", None) or "")[:220],
    }


def _tv_entry(t) -> dict:
    date = getattr(t, "first_air_date", None)
    return {
        "media_type": "tv",
        "tmdb_id": getattr(t, "id", None),
        "title": getattr(t, "name", None) or getattr(t, "original_name", None) or "Untitled",
        "year": getattr(date, "year", None) if date else None,
        "poster": _poster(getattr(t, "poster_path", None)),
        "overview": (getattr(t, "overview", None) or "")[:220],
    }


#----- IMDb/Cinemeta name search (no API key, tried before TMDB)
async def _cinemeta_name_search(query: str) -> list:
    out = []
    for media_type, cm_type in (("movie", "movie"), ("tv", "series")):
        try:
            hits = await search_title_multi(query, cm_type, limit=8)
        except Exception:
            hits = []
        for h in hits:
            if not h.get("id"):
                continue
            out.append({
                "media_type": media_type,
                "tmdb_id": None,
                "imdb_id": h.get("id"),
                "title": h.get("title") or "Untitled",
                "year": extract_first_year(h.get("year")) or None,
                "poster": h.get("poster") or "",
                "overview": "",
            })
    return out


#----- IMDb/Cinemeta lookup by imdb id (also yields tmdb id when known)
async def _cinemeta_id_search(imdb_id: str) -> list:
    out = []
    for media_type, cm_type in (("movie", "movie"), ("tv", "series")):
        try:
            detail = await cinemeta_detail(imdb_id, cm_type)
        except Exception:
            detail = None
        if detail and detail.get("title"):
            mid = detail.get("moviedb_id")
            out.append({
                "media_type": media_type,
                "tmdb_id": int(mid) if str(mid or "").isdigit() else None,
                "imdb_id": detail.get("id") or imdb_id,
                "title": detail.get("title"),
                "year": (detail.get("releaseDetailed") or {}).get("year") or None,
                "poster": detail.get("poster") or "",
                "overview": (detail.get("plot") or "")[:220],
            })
    return out


async def _tmdb_id_search(client, tmdb_id: int) -> list:
    out = []
    try:
        mv = await client.movie(tmdb_id).details()
        if getattr(mv, "title", None):
            out.append(_movie_entry(mv))
    except Exception:
        pass
    try:
        tv = await client.tv(tmdb_id).details()
        if getattr(tv, "name", None):
            out.append(_tv_entry(tv))
    except Exception:
        pass
    return out


async def _tmdb_imdb_search(client, imdb_id: str) -> list:
    out = []
    found = await client.find().by_imdb(imdb_id)
    for mv in (getattr(found, "movie_results", None) or []):
        out.append(_movie_entry(mv))
    for tv in (getattr(found, "tv_results", None) or []):
        out.append(_tv_entry(tv))
    return out


async def _tmdb_name_search(client, query: str) -> list:
    out = []
    multi = await client.search().multi(query)
    for item in (multi or []):
        if getattr(item, "is_movie", False):
            out.append(_movie_entry(item))
        elif getattr(item, "is_tv", False):
            out.append(_tv_entry(item))
    return out


def _dedupe(results: list) -> list:
    seen = set()
    clean = []
    for r in results:
        if not r.get("tmdb_id") and not r.get("imdb_id"):
            continue
        key = (r["media_type"], r.get("imdb_id") or f"tmdb:{r.get('tmdb_id')}")
        if key in seen:
            continue
        seen.add(key)
        clean.append(r)
    return clean[:15]


#----- Search by name/IMDb id/TMDB id. IMDb (Cinemeta) is tried first; TMDB is a fallback.
async def search_titles(query: str) -> list:
    query = (query or "").strip()
    if len(query) < 2:
        return []

    imdb_id = None
    tmdb_id = None
    match = _IMDB_RE.search(query)
    if match:
        imdb_id = match.group(1)
    elif query.isdigit():
        tmdb_id = int(query)
    else:
        found_id = extract_default_id(query)
        if found_id and str(found_id).startswith("tt"):
            imdb_id = str(found_id)
        elif found_id and str(found_id).isdigit():
            tmdb_id = int(found_id)

    try:
        if imdb_id:
            results = await _cinemeta_id_search(imdb_id)
            if not results and tmdb_api_key():
                results = await _tmdb_imdb_search(get_tmdb_client(), imdb_id)
        elif tmdb_id:
            results = await _tmdb_id_search(get_tmdb_client(), tmdb_id) if tmdb_api_key() else []
        else:
            results = await _cinemeta_name_search(query)
            if not results and tmdb_api_key():
                results = await _tmdb_name_search(get_tmdb_client(), query)
    except Exception as e:
        LOGGER.warning(f"[REQUEST] search failed for '{query}': {e}")
        return []

    return _dedupe(results)


#----- Does this title already exist in the library? Check imdb id, then tmdb id, then name.
async def media_exists(media_type: str, tmdb_id, imdb_id, title: str) -> bool:
    media_type = _norm_type(media_type)
    try:
        if imdb_id:
            if await db.get_media_details(imdb_id=imdb_id):
                return True
        if tmdb_id:
            if await db.find_media_doc(media_type, int(tmdb_id)):
                return True
        if title:
            found = await db.search_documents(query=title, page=1, page_size=5)
            target = _norm_title(title)
            for item in (found.get("results") or []):
                if item.get("media_type") == media_type and _norm_title(item.get("title")) == target:
                    return True
    except Exception as e:
        LOGGER.warning(f"[REQUEST] library existence check failed: {e}")
    return False


#----- Public submit: de-duplicated per title, honouring banned/denied/uploaded state
async def submit_request(*, media_type, tmdb_id, imdb_id, title, year, poster, client_ip) -> dict:
    media_type = _norm_type(media_type)
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (TypeError, ValueError):
        tmdb_id = None
    imdb_id = imdb_id or None
    if not tmdb_id and not imdb_id:
        return {"ok": False, "reason": "invalid"}

    #----- Match an existing request by either id (imdb id preferred)
    ors = []
    if imdb_id:
        ors.append({"imdb_id": imdb_id})
    if tmdb_id:
        ors.append({"tmdb_id": tmdb_id})
    now = datetime.utcnow()
    iphash = _hash_ip(client_ip)
    existing = await _coll().find_one({"media_type": media_type, "$or": ors})

    if existing:
        if existing.get("status") == "banned":
            return {"ok": False, "reason": "banned", "title": existing.get("title")}

        update = {"$addToSet": {"requesters": iphash}, "$set": {"last_requested_at": now, "updated_at": now}}
        if imdb_id and not existing.get("imdb_id"):
            update["$set"]["imdb_id"] = imdb_id
        if tmdb_id and not existing.get("tmdb_id"):
            update["$set"]["tmdb_id"] = tmdb_id

        reason = "added"
        if existing.get("status") == "uploaded":
            reason = "already_available"
        elif existing.get("status") == "denied":
            update["$set"]["status"] = "pending"
            reason = "reopened"

        await _coll().update_one({"_id": existing["_id"]}, update)
        return {"ok": True, "reason": reason, "title": existing.get("title")}

    #----- Not requested before: if it's already in the library, no request needed
    if await media_exists(media_type, tmdb_id, imdb_id, title):
        return {"ok": True, "reason": "already_available", "title": title}

    doc = {
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": (title or "Untitled")[:200],
        "year": year,
        "poster": poster or "",
        "status": "pending",
        "requesters": [iphash],
        "created_at": now,
        "updated_at": now,
        "last_requested_at": now,
    }
    await _coll().insert_one(doc)
    return {"ok": True, "reason": "created", "title": doc["title"]}


def _shape(doc: dict) -> dict:
    doc["_id"] = str(doc["_id"])
    doc["request_count"] = len(doc.get("requesters") or [])
    doc.pop("requesters", None)
    return doc


async def list_requests() -> list:
    items = []
    async for doc in _coll().find({}).sort("last_requested_at", -1):
        items.append(_shape(doc))
    return items


async def popular_pending(limit: int = 12) -> list:
    items = [_shape(doc) async for doc in _coll().find({"status": "pending"})]
    items.sort(key=lambda d: d["request_count"], reverse=True)
    return items[:limit]


async def set_status(request_id: str, status: str):
    if status not in STATUSES:
        return None
    try:
        oid = ObjectId(request_id)
    except Exception:
        return None
    doc = await _coll().find_one_and_update(
        {"_id": oid},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    return _shape(doc) if doc else None


async def delete_request(request_id: str) -> bool:
    try:
        oid = ObjectId(request_id)
    except Exception:
        return False
    result = await _coll().delete_one({"_id": oid})
    return result.deleted_count > 0


#----- Mark matching pending requests as uploaded when a title is added to a channel
async def auto_fulfill(tmdb_id=None, imdb_id=None, media_type: str = "movie") -> int:
    media_type = _norm_type(media_type)
    ors = []
    if tmdb_id:
        try:
            ors.append({"tmdb_id": int(tmdb_id)})
        except (TypeError, ValueError):
            pass
    if imdb_id:
        ors.append({"imdb_id": imdb_id})
    if not ors:
        return 0
    result = await _coll().update_many(
        {"media_type": media_type, "status": "pending", "$or": ors},
        {"$set": {"status": "uploaded", "updated_at": datetime.utcnow()}},
    )
    if result.modified_count:
        LOGGER.info(f"[REQUEST] auto-fulfilled {result.modified_count} request(s) for {media_type} tmdb={tmdb_id} imdb={imdb_id}")
    return result.modified_count
