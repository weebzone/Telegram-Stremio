import hashlib
import re
from datetime import datetime

from bson import ObjectId
from pymongo import ReturnDocument

from Backend.helper.request_notifier import notify_new_request
from Backend.helper.external_api_notifier import notify_external_api
from Backend import db
from Backend.helper.metadata.providers.cinemeta import extract_first_year, get_detail as cinemeta_detail, search_title_multi
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


def _year_int(value) -> int:
    match = re.search(r"(\d{4})", str(value or ""))
    return int(match.group(1)) if match else 0


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


#----- TMDB season numbers (regular seasons only, excludes 0/specials)
async def _tmdb_tv_seasons(tmdb_id):
    if not tmdb_id:
        return []
    try:
        client = get_tmdb_client()
        tv = await client.tv(int(tmdb_id)).details()
    except Exception as e:
        LOGGER.warning(f"[REQUEST] tmdb tv seasons failed {tmdb_id}: {e}")
        return []
    out = []
    for s in (getattr(tv, "seasons", None) or []):
        num = getattr(s, "season_number", None)
        if num is None or num == 0:
            continue
        out.append(num)
    return sorted(out)


#----- Cinemeta fallback: derive season numbers from episode list (no API key needed)
async def _cinemeta_tv_seasons(imdb_id):
    if not imdb_id:
        return []
    try:
        detail = await cinemeta_detail(imdb_id, "series")
        videos = (detail or {}).get("videos") or []
        nums = {int(v.get("season")) for v in videos if (v.get("season") or 0) > 0}
        return sorted(nums)
    except Exception as e:
        LOGGER.warning(f"[REQUEST] cinemeta tv seasons failed {imdb_id}: {e}")
        return []


async def _tv_seasons(tmdb_id, imdb_id):
    """Season numbers for a series: prefer TMDB, fall back to Cinemeta."""
    seasons = await _tmdb_tv_seasons(tmdb_id)
    if seasons:
        return seasons
    return await _cinemeta_tv_seasons(imdb_id)


#----- Seasons of this tmdb_id (or imdb_id) that already have >=1 uploaded episode in the DB
async def _db_tv_available_seasons(tmdb_id, imdb_id=None):
    if not tmdb_id and not imdb_id:
        return set()
    doc = None
    try:
        if tmdb_id:
            try:
                res = await db.find_media_doc("tv", int(tmdb_id))
                if res:
                    doc, _ = res
            except (TypeError, ValueError):
                pass
        if not doc and imdb_id:
            # Fallback: search by IMDb id (Cinemeta-only results have no tmdb_id)
            for i in range(1, db.current_db_index + 1):
                coll = db.dbs.get(f"storage_{i}", {}).get("tv")
                if not coll:
                    continue
                doc = await coll.find_one({"imdb_id": imdb_id})
                if doc:
                    doc["db_index"] = i
                    break
    except Exception as e:
        LOGGER.warning(f"[REQUEST] db tv available seasons failed (tmdb={tmdb_id}): {e}")
        return set()
    if not doc:
        return set()
    avail = set()
    for s in doc.get("seasons", []):
        num = s.get("season_number")
        eps = s.get("episodes", [])
        if any(ep.get("telegram") for ep in eps):
            avail.add(num)
    return avail


#----- Per-season availability: {all, available, missing} season-number lists
async def tv_seasons_status(tmdb_id, imdb_id=None):
    all_s = await _tv_seasons(tmdb_id, imdb_id)
    if not all_s:
        return {"all": [], "available": [], "missing": []}
    avail = await _db_tv_available_seasons(tmdb_id, imdb_id)
    available = [s for s in all_s if s in avail]
    missing = [s for s in all_s if s not in avail]
    return {"all": all_s, "available": available, "missing": missing}


def _norm_seasons(value) -> list:
    out = []
    if not value:
        return out
    for s in value:
        try:
            n = int(s)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    # de-dup preserving order
    seen = set()
    uniq = []
    for n in sorted(out):
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


#----- Is a requested set of seasons already fully available in the library?
async def tv_seasons_available(tmdb_id, season_numbers, imdb_id=None) -> bool:
    if not season_numbers:
        return False
    wanted = set(_norm_seasons(season_numbers))
    if not wanted:
        return False
    avail = await _db_tv_available_seasons(tmdb_id, imdb_id)
    return wanted.issubset(avail)


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
async def media_exists(media_type: str, tmdb_id, imdb_id, title: str, year=None) -> bool:
    media_type = _norm_type(media_type)
    try:
        #----- 1) requested IMDb id vs library IMDb id
        if imdb_id and await db.get_media_details(imdb_id=imdb_id):
            return True
        #----- 2) requested TMDB id vs library TMDB id
        if tmdb_id and await db.find_media_doc(media_type, int(tmdb_id)):
            return True
        #----- 3) requested name + year vs library title + release_year
        if title:
            found = await db.search_documents(query=title, page=1, page_size=8)
            target = _norm_title(title)
            want_year = _year_int(year)
            for item in (found.get("results") or []):
                if item.get("media_type") != media_type:
                    continue
                if _norm_title(item.get("title")) != target:
                    continue
                if want_year and _year_int(item.get("release_year")) != want_year:
                    continue
                return True
    except Exception as e:
        LOGGER.warning(f"[REQUEST] library existence check failed: {e}")
    return False


#----- Enrich search results with availability flags so the public Request page
#----- can show "Disponible" / "Parcial" instead of a flat "Request".
async def search_titles_enriched(query: str) -> list:
    results = await search_titles(query)
    for r in results:
        try:
            if r.get("media_type") == "tv" and (r.get("tmdb_id") or r.get("imdb_id")):
                st = await tv_seasons_status(r.get("tmdb_id"), r.get("imdb_id"))
                r["seasons_status"] = st
                if st["all"]:
                    if not st["available"]:
                        r["availability"] = "missing"      # no seasons in library
                    elif len(st["available"]) == len(st["all"]):
                        r["availability"] = "complete"     # all seasons in library
                    else:
                        r["availability"] = "partial"      # some seasons in library
                else:
                    r["availability"] = "unknown"
            else:
                #----- Movies: actually check the library before claiming available
                in_lib = await media_exists(
                    "movie", r.get("tmdb_id"), r.get("imdb_id"),
                    r.get("title", ""), r.get("year"),
                )
                r["availability"] = "complete" if in_lib else "missing"
        except Exception:
            if r.get("media_type") == "tv":
                r["seasons_status"] = {"all": [], "available": [], "missing": []}
                r["availability"] = "unknown"
            else:
                r["availability"] = "missing"
        # legacy flat flag kept for backward-compat (true only if fully available)
        r["available"] = r.get("availability") in ("complete", "movie")
    return results


#----- Public submit: de-duplicated per (title + requested seasons), honouring
#----- banned/denied/uploaded state. For TV, season_numbers drives availability.
async def submit_request(*, media_type, tmdb_id, imdb_id, title, year, poster, client_ip,
                         season_numbers=None, episode_num=None) -> dict:
    media_type = _norm_type(media_type)
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (TypeError, ValueError):
        tmdb_id = None
    imdb_id = imdb_id or None
    seasons = _norm_seasons(season_numbers) if media_type == "tv" else []
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
        # merge requested seasons into the existing request
        new_seasons = []
        if seasons:
            prev = set(existing.get("season_numbers") or [])
            new_seasons = [s for s in seasons if s not in prev]
            if new_seasons:
                update["$addToSet"]["season_numbers"] = {"$each": seasons}

        #----- Honest availability for TV: only "already_available" if the
        #----- specifically requested seasons are actually in the library.
        #----- A blanket "uploaded" status is NOT enough for a partial series.
        #----- reason: "added"  -> new season(s) merged into the request
        #-----         "voted"  -> nothing new, just an extra vote
        if media_type == "tv" and seasons:
            if await tv_seasons_available(tmdb_id, seasons, imdb_id):
                reason = "already_available"
            else:
                # reopen/keep pending so the missing seasons stay requested
                if existing.get("status") != "pending":
                    update["$set"]["status"] = "pending"
                reason = "added" if new_seasons else "voted"
        else:
            if existing.get("status") == "uploaded":
                reason = "already_available"
            elif existing.get("status") == "denied":
                update["$set"]["status"] = "pending"
                reason = "reopened"
            else:
                reason = "voted" if not new_seasons else "added"

        await _coll().update_one({"_id": existing["_id"]}, update)

        #----- If new seasons were actually added to an existing request,
        #----- re-notify Telegram + external API so the new seasons are acted on
        #----- (not just silently merged into the DB). Both targets receive ONE
        #----- message per new season (Telegram + external API share the
        #----- per-season contract your downstream API is built around).
        if new_seasons:
            for s in sorted(new_seasons):
                single = dict(existing)
                single["season_numbers"] = [s]
                single["status"] = (await _coll().find_one({"_id": existing["_id"]}) or {}).get("status", single.get("status"))
                notify_new_request(single)
                notify_external_api(single)

        return {"ok": True, "reason": reason, "title": existing.get("title")}

    #----- Not requested before: check honest availability before creating
    already = False
    if media_type == "tv":
        if seasons:
            already = await tv_seasons_available(tmdb_id, seasons)
        else:
            # no seasons specified -> only "available" if the whole show is in the lib
            st = await tv_seasons_status(tmdb_id, imdb_id)
            already = bool(st["all"]) and len(st["available"]) == len(st["all"])
    else:
        already = await media_exists(media_type, tmdb_id, imdb_id, title, year)
    if already:
        return {"ok": True, "reason": "already_available", "title": title}

    doc = {
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": (title or "Untitled")[:200],
        "year": year,
        "poster": poster or "",
        "season_numbers": seasons,
        "episode_num": episode_num if (seasons and episode_num) else None,
        "status": "pending",
        "requesters": [iphash],
        "created_at": now,
        "updated_at": now,
        "last_requested_at": now,
    }
    await _coll().insert_one(doc)
    notify_new_request(doc)
    notify_external_api(doc)
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


#----- Mark matching pending requests as fulfilled when a title (or season) is added.
#----- For TV: only mark "uploaded" if every season the request asked for is now
#----- in the library; otherwise keep it "pending" for the still-missing seasons.
async def auto_fulfill(tmdb_id=None, imdb_id=None, media_type: str = "movie",
                      season_number=None) -> int:
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

    #----- Movies: straightforward
    if media_type != "tv":
        result = await _coll().update_many(
            {"media_type": "movie", "status": "pending", "$or": ors},
            {"$set": {"status": "uploaded", "updated_at": datetime.utcnow()}},
        )
        if result.modified_count:
            LOGGER.info(f"[REQUEST] auto-fulfilled {result.modified_count} movie request(s)")
        return result.modified_count

    #----- TV: evaluate each pending request for season coverage
    modified = 0
    async for doc in _coll().find({"media_type": "tv", "status": "pending", "$or": ors}):
        wanted = doc.get("season_numbers") or []
        if not wanted:
            # no specific seasons requested -> fulfilled when whole show present
            st = await tv_seasons_status(tmdb_id, imdb_id)
            if st["all"] and len(st["available"]) == len(st["all"]):
                await _coll().update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"status": "uploaded", "updated_at": datetime.utcnow()}},
                )
                modified += 1
            continue
        if season_number is not None and int(season_number) in _norm_seasons(wanted):
            # the just-uploaded season was requested; check if all wanted now present
            if await tv_seasons_available(tmdb_id, wanted):
                await _coll().update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"status": "uploaded", "updated_at": datetime.utcnow()}},
                )
                modified += 1
    if modified:
        LOGGER.info(f"[REQUEST] auto-fulfilled {modified} TV request(s) for tmdb={tmdb_id}")
    return modified
