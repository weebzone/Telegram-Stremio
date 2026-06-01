import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import httpx

from Backend.config import Telegram
from Backend.logger import LOGGER

# -----------------------------
# Auto catalog settings
# -----------------------------
AUTO_CATALOG_REGION = getattr(Telegram, "AUTO_CATALOG_REGION", "IN")
AUTO_CATALOG_ON_STARTUP = getattr(Telegram, "AUTO_CATALOG_ON_STARTUP", True)
AUTO_CATALOG_FULL_REBUILD_ON_STARTUP = getattr(Telegram, "AUTO_CATALOG_FULL_REBUILD_ON_STARTUP", False)
AUTO_SYNC_DELAY_SECONDS = getattr(Telegram, "AUTO_SYNC_DELAY_SECONDS", 20)
AUTO_SYNC_CONCURRENCY = getattr(Telegram, "AUTO_SYNC_CONCURRENCY", 5)

# Hourly quick sync. It only starts after the admin has saved at least one
# auto-catalog option from /catalogs, so first boot stays clean.
AUTO_CATALOG_INTERVAL_SYNC = getattr(Telegram, "AUTO_CATALOG_INTERVAL_SYNC", True)
AUTO_CATALOG_SYNC_INTERVAL_MINUTES = int(getattr(Telegram, "AUTO_CATALOG_SYNC_INTERVAL_MINUTES", 60))

# User can choose exactly which auto catalogs are enabled.
AUTO_CATALOG_DEFINITIONS = [
    {"key": "bollywood", "name": "Bollywood", "group": "Language"},
    {"key": "hollywood", "name": "Hollywood", "group": "Language"},
    {"key": "anime", "name": "Anime", "group": "Language"},
    {"key": "kdrama", "name": "K-Drama", "group": "Language"},
    {"key": "bengali", "name": "Bengali", "group": "Language"},
    {"key": "south_indian", "name": "South Indian", "group": "Language"},
    {"key": "tamil", "name": "Tamil", "group": "Language"},
    {"key": "telugu", "name": "Telugu", "group": "Language"},
    {"key": "malayalam", "name": "Malayalam", "group": "Language"},
    {"key": "kannada", "name": "Kannada", "group": "Language"},
    {"key": "japanese", "name": "Japanese", "group": "Language"},
    {"key": "korean", "name": "Korean", "group": "Language"},

    {"key": "top_rated", "name": "Top Rated", "group": "Smart"},
    {"key": "recently_added", "name": "Recently Added", "group": "Smart"},

    {"key": "netflix", "name": "Netflix", "group": "OTT"},
    {"key": "prime_video", "name": "Prime Video", "group": "OTT"},
    {"key": "hotstar", "name": "Hotstar", "group": "OTT"},
    {"key": "apple_tv", "name": "Apple TV", "group": "OTT"},
    {"key": "hulu", "name": "Hulu", "group": "OTT"},
    {"key": "hbo", "name": "HBO", "group": "OTT"},
    {"key": "jiocinema", "name": "JioCinema", "group": "OTT"},
    {"key": "zee5", "name": "ZEE5", "group": "OTT"},
    {"key": "sonyliv", "name": "SonyLIV", "group": "OTT"},
    {"key": "mx_player", "name": "MX Player", "group": "OTT"},
    {"key": "crunchyroll", "name": "Crunchyroll", "group": "OTT"},
]

CATALOG_BY_NAME = {item["name"]: item for item in AUTO_CATALOG_DEFINITIONS}
CATALOG_BY_KEY = {item["key"]: item for item in AUTO_CATALOG_DEFINITIONS}
DEFAULT_ENABLED_AUTO_CATALOG_KEYS = set(getattr(
    Telegram,
    "AUTO_CATALOG_ENABLED_KEYS",
    [item["key"] for item in AUTO_CATALOG_DEFINITIONS]
))

_LANGUAGE_CATALOGS = {
    "hi": ["Bollywood"],
    "en": ["Hollywood"],
    "ja": ["Japanese"],
    "ko": ["Korean"],
    "bn": ["Bengali"],
    "te": ["South Indian", "Telugu"],
    "ta": ["South Indian", "Tamil"],
    "ml": ["South Indian", "Malayalam"],
    "kn": ["South Indian", "Kannada"],
}

_PROVIDER_ALIASES = {
    "netflix": "Netflix",
    "amazon prime video": "Prime Video",
    "prime video": "Prime Video",
    "amazon video": "Prime Video",
    "hotstar": "Hotstar",
    "disney plus hotstar": "Hotstar",
    "disney+ hotstar": "Hotstar",
    "jiohotstar": "Hotstar",
    "hulu": "Hulu",
    "apple tv": "Apple TV",
    "apple tv plus": "Apple TV",
    "apple tv+": "Apple TV",
    "hbo max": "HBO",
    "max": "HBO",
    "jio cinema": "JioCinema",
    "jiocinema": "JioCinema",
    "zee5": "ZEE5",
    "sony liv": "SonyLIV",
    "sonyliv": "SonyLIV",
    "mx player": "MX Player",
    "crunchyroll": "Crunchyroll",
}

_auto_sync_lock = asyncio.Lock()
_auto_sync_task: Optional[asyncio.Task] = None


def _tmdb_api_key() -> str:
    return getattr(Telegram, "TMDB_API", "") or ""


def _media_type(doc: dict) -> str:
    return "tv" if doc.get("media_type") in ["tv", "series"] else "movie"


def _catalog_key(name: str) -> str:
    value = (name or "").strip().lower().replace("&", "and")
    value = "_".join(value.split())
    value = "".join(ch for ch in value if ch.isalnum() or ch == "_")
    return f"auto_{value}"


def _doc_identity(doc: dict) -> Tuple[str, int, int]:
    return (_media_type(doc), int(doc.get("tmdb_id")), int(doc.get("db_index", 1)))


def _doc_item(doc: dict) -> dict:
    media_type, tmdb_id, db_index = _doc_identity(doc)
    return {
        "tmdb_id": tmdb_id,
        "db_index": db_index,
        "media_type": media_type,
        "added_at": datetime.utcnow(),
    }


def _provider_bucket(provider_name: str) -> Optional[str]:
    value = (provider_name or "").strip().lower()
    if not value:
        return None
    for needle, bucket in _PROVIDER_ALIASES.items():
        if needle in value:
            return bucket
    return None


def _extract_provider_names(watch_data: dict) -> List[str]:
    results = (watch_data or {}).get("results") or {}
    region_data = results.get(AUTO_CATALOG_REGION) or results.get("US") or {}
    names: List[str] = []
    for group in ["flatrate", "ads", "free", "rent", "buy"]:
        for provider in region_data.get(group, []) or []:
            name = provider.get("provider_name")
            if name:
                names.append(name)
    return names


def _is_already_synced(doc: dict) -> bool:
    auto_catalog = doc.get("auto_catalog") or {}
    if not auto_catalog.get("synced"):
        return False

    synced_at = auto_catalog.get("synced_at")
    source_updated_on = auto_catalog.get("source_updated_on")
    doc_updated_on = doc.get("updated_on")

    if not synced_at:
        return False
    if doc_updated_on and source_updated_on and doc_updated_on != source_updated_on:
        return False
    return True


async def has_auto_catalog_settings(db) -> bool:
    """Return True only after the admin saved auto-catalog options at least once."""
    state = await db.dbs["tracking"]["state"].find_one({"_id": "auto_catalog_settings"})
    return bool(state and isinstance(state.get("enabled_keys"), list))


async def get_auto_catalog_settings(db) -> dict:
    state = await db.dbs["tracking"]["state"].find_one({"_id": "auto_catalog_settings"}) or {}
    configured = isinstance(state.get("enabled_keys"), list)

    # Important: first boot should not auto-create catalogs.
    # Until the admin saves settings from /catalogs, enabled_keys stays empty.
    enabled_keys = state.get("enabled_keys") if configured else []

    enabled_set = {key for key in enabled_keys if key in CATALOG_BY_KEY}
    definitions = []
    for item in AUTO_CATALOG_DEFINITIONS:
        definitions.append({
            **item,
            "enabled": item["key"] in enabled_set,
        })

    return {
        "configured": configured,
        "enabled_keys": sorted(enabled_set),
        "definitions": definitions,
        "region": AUTO_CATALOG_REGION,
        "genre_catalogs_removed": True,
        "interval_sync_enabled": bool(AUTO_CATALOG_INTERVAL_SYNC),
        "interval_minutes": AUTO_CATALOG_SYNC_INTERVAL_MINUTES,
    }


async def update_auto_catalog_settings(db, enabled_keys: List[str]) -> dict:
    clean_keys = sorted({str(key) for key in enabled_keys if str(key) in CATALOG_BY_KEY})
    now = datetime.utcnow()
    await db.dbs["tracking"]["state"].update_one(
        {"_id": "auto_catalog_settings"},
        {"$set": {"enabled_keys": clean_keys, "updated_at": now}},
        upsert=True,
    )
    return await get_auto_catalog_settings(db)


async def _enabled_catalog_names(db) -> Set[str]:
    settings = await get_auto_catalog_settings(db)
    keys = settings.get("enabled_keys") or []
    return {CATALOG_BY_KEY[key]["name"] for key in keys if key in CATALOG_BY_KEY}


def classify_media_from_tmdb(doc: dict, details: dict, watch_data: dict, enabled_names: Set[str]) -> dict:
    tags: Set[str] = set()
    providers: Set[str] = set()

    media_type = _media_type(doc)
    original_language = details.get("original_language") or doc.get("original_language") or ""

    origin_country = details.get("origin_country") or []
    production_countries = [
        c.get("iso_3166_1")
        for c in details.get("production_countries", []) or []
        if c.get("iso_3166_1")
    ]

    for tag in _LANGUAGE_CATALOGS.get(original_language, []):
        tags.add(tag)

    if original_language == "hi" and ("IN" in origin_country or "IN" in production_countries or not origin_country):
        tags.add("Bollywood")

    if original_language == "ko" and media_type == "tv":
        tags.add("K-Drama")

    genre_names = [g.get("name", "") for g in details.get("genres", []) or []]
    genre_lower = {g.lower() for g in genre_names}

    keyword_payload = details.get("keywords") or {}
    keywords = keyword_payload.get("keywords") or keyword_payload.get("results") or []
    keyword_names = {k.get("name", "").lower() for k in keywords if isinstance(k, dict)}

    if original_language == "ja" and ("animation" in genre_lower or "anime" in keyword_names):
        tags.add("Anime")

    try:
        if float(doc.get("rating") or 0) >= 7.5:
            tags.add("Top Rated")
    except Exception:
        pass

    if doc.get("release_year"):
        try:
            if int(doc.get("release_year")) >= datetime.utcnow().year - 1:
                tags.add("Recently Added")
        except Exception:
            pass

    for provider_name in _extract_provider_names(watch_data):
        bucket = _provider_bucket(provider_name)
        if bucket:
            providers.add(bucket)
            tags.add(bucket)

    tags = {tag for tag in tags if tag in enabled_names}

    return {
        "original_language": original_language,
        "origin_country": origin_country,
        "production_countries": production_countries,
        "watch_providers": sorted(providers),
        "auto_tags": sorted(tags),
    }


async def _fetch_tmdb_data(client: httpx.AsyncClient, doc: dict) -> tuple[dict, dict]:
    api_key = _tmdb_api_key()
    if not api_key:
        return {}, {}

    media_type = _media_type(doc)
    tmdb_id = doc.get("tmdb_id")

    if not tmdb_id and doc.get("imdb_id"):
        find_url = f"https://api.themoviedb.org/3/find/{doc.get('imdb_id')}"
        params = {"api_key": api_key, "external_source": "imdb_id"}
        resp = await client.get(find_url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            result_key = "tv_results" if media_type == "tv" else "movie_results"
            results = data.get(result_key) or []
            if results:
                tmdb_id = results[0].get("id")

    if not tmdb_id:
        return {}, {}

    detail_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
    detail_params = {
        "api_key": api_key,
        "language": "en-US",
        "append_to_response": "keywords",
    }
    provider_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/watch/providers"

    detail_resp, provider_resp = await asyncio.gather(
        client.get(detail_url, params=detail_params),
        client.get(provider_url, params={"api_key": api_key}),
        return_exceptions=True,
    )

    details = detail_resp.json() if not isinstance(detail_resp, Exception) and detail_resp.status_code == 200 else {}
    providers = provider_resp.json() if not isinstance(provider_resp, Exception) and provider_resp.status_code == 200 else {}
    return details, providers


async def _iter_all_media(db, *, full_rebuild: bool = False):
    for db_index in range(1, db.current_db_index + 1):
        db_key = f"storage_{db_index}"
        if db_key not in db.dbs:
            continue

        for collection_name in ["movie", "tv"]:
            cursor = db.dbs[db_key][collection_name].find({"tmdb_id": {"$exists": True, "$ne": None}})
            async for doc in cursor:
                doc["db_index"] = int(doc.get("db_index", db_index))
                doc["media_type"] = "tv" if collection_name == "tv" else "movie"
                if not full_rebuild and _is_already_synced(doc):
                    yield collection_name, db_index, doc, True
                else:
                    yield collection_name, db_index, doc, False


async def _classify_one(db, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, doc: dict, enabled_names: Set[str]) -> tuple[dict, dict]:
    async with semaphore:
        try:
            details, watch_data = await _fetch_tmdb_data(client, doc)
            classification = classify_media_from_tmdb(doc, details, watch_data, enabled_names) if details else {
                "original_language": doc.get("original_language", ""),
                "origin_country": doc.get("origin_country", []),
                "production_countries": doc.get("production_countries", []),
                "watch_providers": doc.get("watch_providers", []),
                "auto_tags": [tag for tag in (doc.get("auto_tags", []) or []) if tag in enabled_names],
            }

            now = datetime.utcnow()
            update_data = {
                "original_language": classification.get("original_language"),
                "origin_country": classification.get("origin_country", []),
                "production_countries": classification.get("production_countries", []),
                "watch_providers": classification.get("watch_providers", []),
                "auto_tags": classification.get("auto_tags", []),
                "auto_tags_updated_at": now,
                "auto_catalog": {
                    "synced": True,
                    "synced_at": now,
                    "source_updated_on": doc.get("updated_on"),
                },
            }
            await db.update_document(_media_type(doc), int(doc.get("tmdb_id")), int(doc.get("db_index", 1)), update_data)
            return doc, classification
        except Exception as e:
            LOGGER.warning(f"Auto catalog classification failed for {doc.get('title')} ({doc.get('tmdb_id')}): {e}")
            return doc, {"auto_tags": [tag for tag in (doc.get("auto_tags", []) or []) if tag in enabled_names]}


async def _flush_quick_items(db, catalog_items: Dict[str, List[dict]]) -> None:
    collection = db.dbs["tracking"]["custom_catalogs"]
    now = datetime.utcnow()

    for name, items in catalog_items.items():
        auto_key = _catalog_key(name)
        await collection.update_one(
            {"auto_key": auto_key},
            {
                "$setOnInsert": {
                    "name": name,
                    "visible": True,
                    "auto": True,
                    "auto_key": auto_key,
                    "items": [],
                    "item_count": 0,
                    "created_at": now,
                },
                "$set": {
                    "updated_at": now,
                    "last_auto_sync": now,
                },
            },
            upsert=True,
        )

        for item in items:
            await collection.update_one(
                {
                    "auto_key": auto_key,
                    "items": {
                        "$not": {
                            "$elemMatch": {
                                "tmdb_id": item["tmdb_id"],
                                "db_index": item["db_index"],
                                "media_type": item["media_type"],
                            }
                        }
                    },
                },
                {
                    "$push": {"items": {"$each": [item], "$position": 0}},
                    "$set": {"updated_at": now},
                    "$inc": {"item_count": 1},
                },
            )


async def _rebuild_auto_catalogs(db, catalog_items: Dict[str, List[dict]], enabled_names: Set[str]) -> None:
    collection = db.dbs["tracking"]["custom_catalogs"]
    now = datetime.utcnow()

    for name, items in catalog_items.items():
        seen = set()
        unique_items = []
        for item in items:
            key = (item["media_type"], item["tmdb_id"], item["db_index"])
            if key in seen:
                continue
            seen.add(key)
            unique_items.append(item)

        await collection.update_one(
            {"auto_key": _catalog_key(name)},
            {
                "$set": {
                    "name": name,
                    "visible": True,
                    "auto": True,
                    "auto_key": _catalog_key(name),
                    "items": unique_items,
                    "item_count": len(unique_items),
                    "updated_at": now,
                    "last_auto_sync": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )


    active_keys = {_catalog_key(name) for name in enabled_names}
    await collection.update_many(
        {"auto": True, "auto_key": {"$nin": list(active_keys)}},
        {"$set": {"visible": False, "items": [], "item_count": 0, "updated_at": now}},
    )


async def _write_status(db, data: dict) -> None:
    await db.dbs["tracking"]["state"].update_one(
        {"_id": "auto_catalog_sync"},
        {"$set": data},
        upsert=True,
    )


async def run_auto_catalog_sync(db, *, force: bool = False, full_rebuild: bool = False, delay_seconds: int = 0) -> dict:
    if delay_seconds:
        await asyncio.sleep(delay_seconds)

    if _auto_sync_lock.locked() and not force:
        return {"running": True, "message": "Auto catalog sync is already running."}

    async with _auto_sync_lock:
        started_at = datetime.utcnow()
        await _write_status(db, {
            "running": True,
            "mode": "full_rebuild" if full_rebuild else "quick_sync",
            "message": "Auto catalog sync running...",
            "started_at": started_at,
            "finished_at": None,
            "scanned": 0,
            "skipped": 0,
            "classified": 0,
            "tagged": 0,
            "catalogs": 0,
        })

        catalog_items: Dict[str, List[dict]] = {}
        scanned = 0
        skipped = 0
        classified = 0
        tagged = 0
        error_message = None
        settings_configured = await has_auto_catalog_settings(db)
        enabled_names = await _enabled_catalog_names(db)

        if not settings_configured or not enabled_names:
            finished_at = datetime.utcnow()
            summary = {
                "running": False,
                "mode": "full_rebuild" if full_rebuild else "quick_sync",
                "message": "Auto catalog skipped. Save auto catalog selection first.",
                "scanned": 0,
                "skipped": 0,
                "classified": 0,
                "tagged": 0,
                "catalogs": 0,
                "enabled_catalogs": [],
                "started_at": started_at,
                "finished_at": finished_at,
                "error": None,
            }
            await _write_status(db, summary)
            LOGGER.info(f"Auto catalog sync skipped: {summary}")
            return summary

        try:
            timeout = httpx.Timeout(18.0, connect=10.0)
            limits = httpx.Limits(max_connections=AUTO_SYNC_CONCURRENCY + 2, max_keepalive_connections=AUTO_SYNC_CONCURRENCY)
            semaphore = asyncio.Semaphore(AUTO_SYNC_CONCURRENCY)

            async def consume_result(media_doc: dict, classification: dict) -> None:
                nonlocal tagged
                tags = classification.get("auto_tags", []) or []
                if tags:
                    tagged += 1
                item = _doc_item(media_doc)
                for tag in tags:
                    if tag in enabled_names:
                        catalog_items.setdefault(tag, []).append(item)

            async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
                pending = []
                async for _, _, doc, already_synced in _iter_all_media(db, full_rebuild=full_rebuild):
                    scanned += 1

                    if already_synced and not force and not full_rebuild:
                        skipped += 1
                        continue

                    if already_synced and force and not full_rebuild:
                        skipped += 1
                        continue

                    classified += 1
                    pending.append(_classify_one(db, client, semaphore, doc, enabled_names))

                    if len(pending) >= 40:
                        for media_doc, classification in await asyncio.gather(*pending):
                            await consume_result(media_doc, classification)
                        pending = []
                        await _write_status(db, {
                            "running": True,
                            "mode": "full_rebuild" if full_rebuild else "quick_sync",
                            "started_at": started_at,
                            "scanned": scanned,
                            "skipped": skipped,
                            "classified": classified,
                            "tagged": tagged,
                            "catalogs": len(catalog_items),
                        })

                if pending:
                    for media_doc, classification in await asyncio.gather(*pending):
                        await consume_result(media_doc, classification)

            if full_rebuild:
                await _rebuild_auto_catalogs(db, catalog_items, enabled_names)
            else:
                await _flush_quick_items(db, catalog_items)

            finished_at = datetime.utcnow()
            summary = {
                "running": False,
                "mode": "full_rebuild" if full_rebuild else "quick_sync",
                "scanned": scanned,
                "skipped": skipped,
                "classified": classified,
                "tagged": tagged,
                "catalogs": len(catalog_items),
                "enabled_catalogs": sorted(enabled_names),
                "started_at": started_at,
                "finished_at": finished_at,
                "error": None,
            }
            await _write_status(db, summary)
            LOGGER.info(f"Auto catalog sync complete: {summary}")
            return summary
        except Exception as exc:
            error_message = str(exc)
            finished_at = datetime.utcnow()
            summary = {
                "running": False,
                "mode": "full_rebuild" if full_rebuild else "quick_sync",
                "scanned": scanned,
                "skipped": skipped,
                "classified": classified,
                "tagged": tagged,
                "catalogs": len(catalog_items),
                "enabled_catalogs": sorted(enabled_names),
                "started_at": started_at,
                "finished_at": finished_at,
                "error": error_message,
            }
            await _write_status(db, summary)
            LOGGER.error(f"Auto catalog sync failed: {summary}")
            raise


async def start_auto_catalog_sync_background(db, *, full_rebuild: bool = False, force: bool = False, delay_seconds: int = 0) -> dict:
    global _auto_sync_task

    if _auto_sync_lock.locked() or (_auto_sync_task and not _auto_sync_task.done()):
        return {"running": True, "message": "Auto catalog sync is already running."}

    started_at = datetime.utcnow()
    await _write_status(db, {
        "running": True,
        "mode": "full_rebuild" if full_rebuild else "quick_sync",
        "message": "Auto catalog sync queued...",
        "started_at": started_at,
        "finished_at": None,
        "scanned": 0,
        "skipped": 0,
        "classified": 0,
        "tagged": 0,
        "catalogs": 0,
    })

    async def runner():
        try:
            await run_auto_catalog_sync(db, force=force, full_rebuild=full_rebuild, delay_seconds=delay_seconds)
        except Exception:
            LOGGER.exception("Background auto catalog sync crashed")

    _auto_sync_task = asyncio.create_task(runner())
    return {
        "running": True,
        "message": "Full rebuild started in background." if full_rebuild else "Quick sync started in background.",
        "mode": "full_rebuild" if full_rebuild else "quick_sync",
        "started_at": started_at,
    }


async def start_auto_catalog_interval_loop(db) -> None:
    """Run quick sync every N minutes after auto-catalog settings exist.

    This avoids per-upload TMDb calls and prevents first boot from creating
    catalogs until the admin chooses options from /catalogs.
    """
    if not AUTO_CATALOG_INTERVAL_SYNC:
        LOGGER.info("Auto catalog interval sync disabled.")
        return

    interval_minutes = max(1, int(AUTO_CATALOG_SYNC_INTERVAL_MINUTES or 60))
    interval_seconds = interval_minutes * 60
    LOGGER.info(f"Auto catalog interval sync loop started. Interval: {interval_minutes} minutes")

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            if not await has_auto_catalog_settings(db):
                LOGGER.info("Hourly auto catalog quick sync skipped: no auto catalog selection saved yet.")
                continue

            if _auto_sync_lock.locked() or (_auto_sync_task and not _auto_sync_task.done()):
                LOGGER.info("Hourly auto catalog quick sync skipped: another sync is already running.")
                continue

            result = await start_auto_catalog_sync_background(
                db,
                full_rebuild=False,
                force=False,
                delay_seconds=0,
            )
            LOGGER.info(f"Hourly auto catalog quick sync queued: {result}")

        except asyncio.CancelledError:
            LOGGER.info("Auto catalog interval sync loop stopped.")
            break
        except Exception as exc:
            LOGGER.error(f"Hourly auto catalog quick sync failed: {exc}")
            await asyncio.sleep(300)


async def get_auto_catalog_sync_status(db) -> dict:
    state = await db.dbs["tracking"]["state"].find_one({"_id": "auto_catalog_sync"}) or {}
    state.pop("_id", None)
    return state
