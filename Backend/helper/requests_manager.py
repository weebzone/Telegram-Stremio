import hashlib
import re
from datetime import datetime

from bson import ObjectId
from pymongo import ReturnDocument

from Backend import db
from Backend.helper.metadata import (
    extract_default_id,
    format_tmdb_image,
    get_tmdb_client,
    tmdb_api_key,
)
from Backend.logger import LOGGER

STATUSES = ("pending", "uploaded", "denied", "banned")
_IMDB_RE = re.compile(r"(tt\d{7,10})")


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


#----- Search TMDB by name, IMDb id (tt...) or TMDB numeric id
async def search_titles(query: str) -> list:
    query = (query or "").strip()
    if len(query) < 2 or not tmdb_api_key():
        return []

    client = get_tmdb_client()
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

    results = []
    try:
        if imdb_id:
            found = await client.find().by_imdb(imdb_id)
            for mv in (getattr(found, "movie_results", None) or []):
                results.append(_movie_entry(mv))
            for tv in (getattr(found, "tv_results", None) or []):
                results.append(_tv_entry(tv))
        elif tmdb_id:
            try:
                mv = await client.movie(tmdb_id).details()
                if getattr(mv, "title", None):
                    results.append(_movie_entry(mv))
            except Exception:
                pass
            try:
                tv = await client.tv(tmdb_id).details()
                if getattr(tv, "name", None):
                    results.append(_tv_entry(tv))
            except Exception:
                pass
        else:
            multi = await client.search().multi(query)
            for item in (multi or []):
                if getattr(item, "is_movie", False):
                    results.append(_movie_entry(item))
                elif getattr(item, "is_tv", False):
                    results.append(_tv_entry(item))
    except Exception as e:
        LOGGER.warning(f"[REQUEST] search failed for '{query}': {e}")
        return []

    seen = set()
    clean = []
    for r in results:
        if not r["tmdb_id"]:
            continue
        key = (r["media_type"], r["tmdb_id"])
        if key in seen:
            continue
        seen.add(key)
        clean.append(r)
    return clean[:15]


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

    query = {"media_type": media_type, ("tmdb_id" if tmdb_id else "imdb_id"): (tmdb_id or imdb_id)}
    now = datetime.utcnow()
    iphash = _hash_ip(client_ip)
    existing = await _coll().find_one(query)

    if existing:
        if existing.get("status") == "banned":
            return {"ok": False, "reason": "banned", "title": existing.get("title")}

        update = {"$addToSet": {"requesters": iphash}, "$set": {"last_requested_at": now, "updated_at": now}}
        if imdb_id and not existing.get("imdb_id"):
            update["$set"]["imdb_id"] = imdb_id

        reason = "added"
        if existing.get("status") == "uploaded":
            reason = "already_available"
        elif existing.get("status") == "denied":
            update["$set"]["status"] = "pending"
            reason = "reopened"

        await _coll().update_one({"_id": existing["_id"]}, update)
        return {"ok": True, "reason": reason, "title": existing.get("title")}

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
