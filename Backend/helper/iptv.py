import asyncio
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from pymongo import ASCENDING, ReplaceOne

from Backend.config import Telegram
from Backend.logger import LOGGER


IPTV_STATE_ID = "iptv_sync_state"
IPTV_SETTINGS_ID = "iptv_settings"
IPTV_CATALOG_ID = "iptv_india"
IPTV_ID_PREFIX = "iptv:"
IPTV_DATASETS = (
    "channels",
    "feeds",
    "logos",
    "streams",
    "categories",
    "languages",
    "countries",
    "blocklist",
)

_sync_lock = asyncio.Lock()
_sync_task: Optional[asyncio.Task] = None


def _utcnow() -> datetime:
    return datetime.utcnow()


def _api_url(name: str) -> str:
    return f"{Telegram.IPTV_API_BASE_URL}/{name}.json"


def _stream_id(stream: dict) -> str:
    identity = "\n".join(
        [
            str(stream.get("url") or ""),
            str(stream.get("referrer") or ""),
            str(stream.get("user_agent") or ""),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _supported_stream_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def channel_is_eligible(channel: dict, country_codes: set, blocked_ids: set) -> bool:
    channel_id = str(channel.get("id") or "")
    return bool(
        channel_id
        and str(channel.get("country") or "").upper() in country_codes
        and not channel.get("is_nsfw")
        and not channel.get("closed")
        and not channel.get("replaced_by")
        and channel_id not in blocked_ids
    )


def _quality_score(value: str) -> int:
    text = str(value or "").lower()
    if "2160" in text or "4k" in text:
        return 2160
    match = re.search(r"(\d{3,4})", text)
    if match:
        return int(match.group(1))
    if "hd" in text:
        return 720
    if "sd" in text:
        return 480
    return 0


def _stream_rank(stream: dict, feed: Optional[dict]) -> Tuple[int, int, int, int]:
    url = str(stream.get("url") or "")
    quality = stream.get("quality") or (feed or {}).get("format") or ""
    no_headers = not stream.get("referrer") and not stream.get("user_agent")
    is_https = url.lower().startswith("https://")
    is_hls = ".m3u8" in url.lower()
    return (
        _quality_score(quality),
        1 if no_headers else 0,
        1 if is_https else 0,
        1 if is_hls else 0,
    )


def _pick_logo(channel_id: str, stream_feeds: set, logos_by_channel: Dict[str, List[dict]]) -> str:
    logos = logos_by_channel.get(channel_id) or []
    if not logos:
        return ""

    def logo_score(logo: dict) -> tuple:
        feed = logo.get("feed")
        feed_match = 2 if feed and feed in stream_feeds else 1 if not feed else 0
        area = int(logo.get("width") or 0) * int(logo.get("height") or 0)
        return feed_match, area

    selected = max(logos, key=logo_score)
    return str(selected.get("url") or "")


def _channel_description(channel: dict, feeds: List[dict], country_name: str) -> str:
    parts = []
    categories = channel.get("categories") or []
    if categories:
        parts.append(", ".join(str(item) for item in categories))
    languages = []
    for feed in feeds:
        for language in feed.get("language_names") or []:
            if language not in languages:
                languages.append(language)
    if languages:
        parts.append("Languages: " + ", ".join(languages))
    if country_name:
        parts.append(country_name)
    return " | ".join(parts) or "Live television channel"


async def get_iptv_settings(db) -> dict:
    state = await db.dbs["tracking"]["state"].find_one({"_id": IPTV_SETTINGS_ID}) or {}
    enabled = state.get("enabled")
    return {
        "enabled": Telegram.IPTV_ENABLED if enabled is None else bool(enabled),
        "country_codes": list(Telegram.IPTV_COUNTRY_CODES),
        "channel_limit": 0,
        "unlimited": True,
        "auto_sync": bool(Telegram.IPTV_AUTO_SYNC),
        "sync_interval_minutes": int(Telegram.IPTV_SYNC_INTERVAL_MINUTES),
        "proxy_fallback_enabled": bool(Telegram.IPTV_PROXY_FALLBACK_ENABLED),
        "catalog_id": IPTV_CATALOG_ID,
    }


async def update_iptv_settings(db, payload: dict) -> dict:
    update = {"updated_at": _utcnow()}
    if "enabled" in payload:
        update["enabled"] = bool(payload.get("enabled"))
    await db.dbs["tracking"]["state"].update_one(
        {"_id": IPTV_SETTINGS_ID},
        {"$set": update, "$unset": {"channel_limit": ""}},
        upsert=True,
    )
    return await get_iptv_settings(db)


async def get_iptv_sync_status(db) -> dict:
    state = await db.dbs["tracking"]["state"].find_one({"_id": IPTV_STATE_ID}) or {}
    settings = await get_iptv_settings(db)
    total_channels = await db.dbs["tracking"]["iptv_channels"].count_documents({})
    visible_channels = await db.dbs["tracking"]["iptv_channels"].count_documents({"hidden": {"$ne": True}})
    state.pop("_id", None)
    return {
        "status": state.get("status", "never_synced"),
        "running": bool(state.get("running", False)),
        "last_started_at": state.get("last_started_at"),
        "last_completed_at": state.get("last_completed_at"),
        "last_error": state.get("last_error"),
        "source_etag": state.get("source_etag"),
        "source_last_modified": state.get("source_last_modified"),
        "sync_id": state.get("sync_id"),
        "counts": {
            **(state.get("counts") or {}),
            "total_channels": total_channels,
            "visible_channels": visible_channels,
            "hidden_channels": max(0, total_channels - visible_channels),
        },
        "settings": settings,
    }


async def _fetch_json(client: httpx.AsyncClient, dataset: str) -> list:
    response = await client.get(_api_url(dataset))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected {dataset}.json response")
    return data


async def sync_iptv_data(db, force: bool = False) -> dict:
    if _sync_lock.locked():
        return {"started": False, "reason": "sync_already_running"}

    async with _sync_lock:
        settings = await get_iptv_settings(db)
        if not settings["enabled"] and not force:
            return {"started": False, "reason": "iptv_disabled"}

        now = _utcnow()
        state_collection = db.dbs["tracking"]["state"]
        await state_collection.update_one(
            {"_id": IPTV_SETTINGS_ID},
            {"$unset": {"channel_limit": ""}},
            upsert=True,
        )
        previous = await state_collection.find_one({"_id": IPTV_STATE_ID}) or {}
        await state_collection.update_one(
            {"_id": IPTV_STATE_ID},
            {
                "$set": {
                    "status": "running",
                    "running": True,
                    "last_started_at": now,
                    "last_error": None,
                }
            },
            upsert=True,
        )

        timeout = httpx.Timeout(Telegram.IPTV_REQUEST_TIMEOUT_SEC)
        headers = {
            "User-Agent": "Telegram-Stremio-IPTV/1.0",
            "Accept": "application/json",
        }
        if not force:
            if previous.get("source_etag"):
                headers["If-None-Match"] = str(previous["source_etag"])
            if previous.get("source_last_modified"):
                headers["If-Modified-Since"] = str(previous["source_last_modified"])

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                stream_response = await client.get(_api_url("streams"), headers=headers)
                if stream_response.status_code == 304:
                    completed = _utcnow()
                    await state_collection.update_one(
                        {"_id": IPTV_STATE_ID},
                        {
                            "$set": {
                                "status": "unchanged",
                                "running": False,
                                "last_completed_at": completed,
                                "last_error": None,
                            }
                        },
                        upsert=True,
                    )
                    return {"started": True, "status": "unchanged"}

                stream_response.raise_for_status()
                all_streams = stream_response.json()
                if not isinstance(all_streams, list):
                    raise ValueError("Unexpected streams.json response")

                channels_raw = await _fetch_json(client, "channels")
                blocklist_raw = await _fetch_json(client, "blocklist")
                country_codes = set(settings["country_codes"])
                blocked_ids = {
                    str(item.get("channel"))
                    for item in blocklist_raw
                    if item.get("channel")
                }

                eligible_channels = {
                    str(item["id"]): item
                    for item in channels_raw
                    if channel_is_eligible(item, country_codes, blocked_ids)
                }
                channels = dict(eligible_channels)
                channel_ids = set(channels)

                streams = [
                    item
                    for item in all_streams
                    if str(item.get("channel") or "") in channel_ids
                    and _supported_stream_url(item.get("url"))
                ]
                active_channel_ids = {str(item.get("channel")) for item in streams}
                channels = {
                    channel_id: item
                    for channel_id, item in channels.items()
                    if channel_id in active_channel_ids
                }
                channel_ids = set(channels)

                feeds_raw = await _fetch_json(client, "feeds")
                feeds = [
                    item for item in feeds_raw
                    if str(item.get("channel") or "") in channel_ids
                ]
                logos_raw = await _fetch_json(client, "logos")
                logos = [
                    item for item in logos_raw
                    if str(item.get("channel") or "") in channel_ids
                ]
                categories_raw = await _fetch_json(client, "categories")
                languages_raw = await _fetch_json(client, "languages")
                countries_raw = await _fetch_json(client, "countries")

            category_names = {
                str(item.get("id")): str(item.get("name") or item.get("id") or "")
                for item in categories_raw
                if item.get("id")
            }
            language_names = {
                str(item.get("code")): str(item.get("name") or item.get("code") or "")
                for item in languages_raw
                if item.get("code")
            }
            country_names = {
                str(item.get("code")): {
                    "name": str(item.get("name") or item.get("code") or ""),
                    "flag": str(item.get("flag") or ""),
                }
                for item in countries_raw
                if item.get("code")
            }

            feeds_by_key: Dict[tuple, dict] = {}
            feeds_by_channel: Dict[str, List[dict]] = {}
            for feed in feeds:
                channel_id = str(feed.get("channel") or "")
                feed_id = str(feed.get("id") or "")
                enriched = dict(feed)
                enriched["language_names"] = [
                    language_names.get(str(code), str(code))
                    for code in feed.get("languages") or []
                ]
                feeds_by_key[(channel_id, feed_id)] = enriched
                feeds_by_channel.setdefault(channel_id, []).append(enriched)

            logos_by_channel: Dict[str, List[dict]] = {}
            for logo in logos:
                logos_by_channel.setdefault(str(logo.get("channel") or ""), []).append(logo)

            streams_by_channel: Dict[str, List[dict]] = {}
            for stream in streams:
                channel_id = str(stream.get("channel") or "")
                feed_id = str(stream.get("feed") or "")
                feed = feeds_by_key.get((channel_id, feed_id))
                quality = stream.get("quality") or (feed or {}).get("format") or "Live"
                request_headers = {}
                if stream.get("referrer"):
                    request_headers["Referer"] = str(stream["referrer"])
                if stream.get("user_agent"):
                    request_headers["User-Agent"] = str(stream["user_agent"])

                item = {
                    "id": _stream_id(stream),
                    "url": str(stream.get("url")),
                    "title": str(stream.get("title") or ""),
                    "quality": str(quality),
                    "feed": feed_id or None,
                    "feed_name": (feed or {}).get("name") or feed_id or None,
                    "is_main_feed": bool((feed or {}).get("is_main", False)),
                    "languages": (feed or {}).get("language_names") or [],
                    "format": (feed or {}).get("format"),
                    "request_headers": request_headers,
                    "requires_headers": bool(request_headers),
                    "rank": _stream_rank(stream, feed),
                }
                streams_by_channel.setdefault(channel_id, []).append(item)

            existing_hidden = {
                str(item["_id"]): bool(item.get("hidden"))
                async for item in db.dbs["tracking"]["iptv_channels"].find(
                    {"hidden": True}, {"_id": 1, "hidden": 1}
                )
            }

            sync_id = hashlib.sha256(
                f"{now.isoformat()}:{stream_response.headers.get('etag', '')}".encode("utf-8")
            ).hexdigest()[:16]
            documents = []
            for channel_id, channel in channels.items():
                channel_streams = sorted(
                    streams_by_channel.get(channel_id, []),
                    key=lambda item: tuple(item.get("rank") or (0, 0, 0, 0)),
                    reverse=True,
                )[:5]
                if not channel_streams:
                    continue

                channel_feeds = feeds_by_channel.get(channel_id, [])
                stream_feed_ids = {
                    str(item.get("feed"))
                    for item in channel_streams
                    if item.get("feed")
                }
                country = country_names.get(str(channel.get("country") or ""), {})
                category_ids = [str(item) for item in channel.get("categories") or []]
                category_values = [
                    category_names.get(category_id, category_id)
                    for category_id in category_ids
                ]
                language_values = []
                for feed in channel_feeds:
                    for language in feed.get("language_names") or []:
                        if language not in language_values:
                            language_values.append(language)

                documents.append(
                    {
                        "_id": channel_id,
                        "stremio_id": f"{IPTV_ID_PREFIX}{channel_id}",
                        "name": str(channel.get("name") or channel_id),
                        "alt_names": channel.get("alt_names") or [],
                        "network": channel.get("network"),
                        "country": str(channel.get("country") or ""),
                        "country_name": country.get("name") or "",
                        "country_flag": country.get("flag") or "",
                        "categories": category_values,
                        "category_ids": category_ids,
                        "languages": language_values,
                        "website": channel.get("website"),
                        "logo": _pick_logo(channel_id, stream_feed_ids, logos_by_channel),
                        "description": _channel_description(channel, channel_feeds, country.get("name") or ""),
                        "hidden": existing_hidden.get(channel_id, False),
                        "streams": channel_streams,
                        "stream_count": len(channel_streams),
                        "sync_id": sync_id,
                        "updated_at": now,
                    }
                )

            documents.sort(
                key=lambda item: (
                    max(
                        [tuple(stream.get("rank") or (0, 0, 0, 0)) for stream in item.get("streams", [])]
                        or [(0, 0, 0, 0)]
                    ),
                    item.get("stream_count", 0),
                    item.get("name", "").lower(),
                ),
                reverse=True,
            )
            documents.sort(key=lambda item: item.get("name", "").lower())

            collection = db.dbs["tracking"]["iptv_channels"]
            if documents:
                await collection.bulk_write(
                    [ReplaceOne({"_id": item["_id"]}, item, upsert=True) for item in documents],
                    ordered=False,
                )
            await collection.delete_many({"sync_id": {"$ne": sync_id}})
            await collection.create_index([("name", ASCENDING)])
            await collection.create_index([("hidden", ASCENDING), ("name", ASCENDING)])
            await collection.create_index("streams.id")

            direct_streams = sum(
                1
                for item in documents
                for stream in item.get("streams", [])
                if not stream.get("requires_headers")
            )
            header_streams = sum(
                1
                for item in documents
                for stream in item.get("streams", [])
                if stream.get("requires_headers")
            )
            completed = _utcnow()
            counts = {
                "source_country_channels": len(eligible_channels),
                "playable_country_channels": len(channels),
                "source_channels": len(eligible_channels),
                "source_streams": len(streams),
                "selected_channels": len(documents),
                "selected_streams": direct_streams + header_streams,
                "direct_streams": direct_streams,
                "header_streams": header_streams,
                "blocked_channels": len(blocked_ids),
                "channel_limit": 0,
                "unlimited": True,
            }
            await state_collection.update_one(
                {"_id": IPTV_STATE_ID},
                {
                    "$set": {
                        "status": "ok",
                        "running": False,
                        "last_completed_at": completed,
                        "last_error": None,
                        "source_etag": stream_response.headers.get("etag"),
                        "source_last_modified": stream_response.headers.get("last-modified"),
                        "sync_id": sync_id,
                        "counts": counts,
                    }
                },
                upsert=True,
            )
            LOGGER.info(
                "IPTV sync complete: %s channels, %s streams",
                counts["selected_channels"],
                counts["selected_streams"],
            )
            return {"started": True, "status": "ok", "counts": counts}
        except Exception as exc:
            LOGGER.exception("IPTV sync failed")
            await state_collection.update_one(
                {"_id": IPTV_STATE_ID},
                {
                    "$set": {
                        "status": "error",
                        "running": False,
                        "last_completed_at": _utcnow(),
                        "last_error": str(exc)[:500],
                    }
                },
                upsert=True,
            )
            return {"started": True, "status": "error", "error": str(exc)}


async def start_iptv_sync_background(db, force: bool = False, delay_seconds: int = 0) -> dict:
    global _sync_task
    if _sync_task and not _sync_task.done():
        return {"started": False, "reason": "sync_already_running"}

    async def runner():
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        await sync_iptv_data(db, force=force)

    _sync_task = asyncio.create_task(runner())
    return {"started": True}


async def start_iptv_interval_loop(db) -> None:
    if not Telegram.IPTV_AUTO_SYNC:
        return
    interval = max(30, int(Telegram.IPTV_SYNC_INTERVAL_MINUTES)) * 60
    while True:
        try:
            await asyncio.sleep(interval)
            settings = await get_iptv_settings(db)
            if settings.get("enabled"):
                await sync_iptv_data(db, force=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("IPTV interval sync failed")


async def list_iptv_channels(
    db,
    *,
    search: str = "",
    category: str = "",
    hidden: Optional[bool] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    query = {}
    if hidden is not None:
        query["hidden"] = bool(hidden)
    search = (search or "").strip()
    category = (category or "").strip()
    if category:
        query["categories"] = category
    if search:
        query["$or"] = [
            {"name": {"$regex": re.escape(search), "$options": "i"}},
            {"alt_names": {"$regex": re.escape(search), "$options": "i"}},
            {"languages": {"$regex": re.escape(search), "$options": "i"}},
            {"categories": {"$regex": re.escape(search), "$options": "i"}},
        ]
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    total = await db.dbs["tracking"]["iptv_channels"].count_documents(query)
    items = await (
        db.dbs["tracking"]["iptv_channels"]
        .find(query)
        .sort("name", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list(None)
    )
    for item in items:
        item["_id"] = str(item["_id"])
    return {
        "channels": items,
        "total_count": total,
        "current_page": page,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
    }


async def get_iptv_channel(db, channel_id: str, include_hidden: bool = False) -> Optional[dict]:
    query = {"_id": channel_id}
    if not include_hidden:
        query["hidden"] = {"$ne": True}
    return await db.dbs["tracking"]["iptv_channels"].find_one(query)


async def get_iptv_channel_by_stream(db, stream_id: str) -> Optional[dict]:
    return await db.dbs["tracking"]["iptv_channels"].find_one(
        {"streams.id": stream_id, "hidden": {"$ne": True}}
    )


async def set_iptv_channel_hidden(db, channel_id: str, hidden: bool) -> bool:
    result = await db.dbs["tracking"]["iptv_channels"].update_one(
        {"_id": channel_id},
        {"$set": {"hidden": bool(hidden), "updated_at": _utcnow()}},
    )
    return result.matched_count > 0


def iptv_meta(channel: dict) -> dict:
    return {
        "id": channel.get("stremio_id") or f"{IPTV_ID_PREFIX}{channel.get('_id')}",
        "type": "tv",
        "name": channel.get("name") or str(channel.get("_id")),
        "description": channel.get("description") or "Live television channel",
        "poster": channel.get("logo") or "",
        "logo": channel.get("logo") or "",
        "background": channel.get("logo") or "",
        "genres": channel.get("categories") or [],
        "country": channel.get("country_name") or channel.get("country") or "",
        "language": ", ".join(channel.get("languages") or []),
        "website": channel.get("website") or "",
    }


def _proxy_secret() -> bytes:
    configured = (
        Telegram.IPTV_PROXY_SECRET
        or Telegram.BOT_TOKEN
        or Telegram.DEFAULT_ADDON_TOKEN
        or "telegram-stremio-iptv"
    )
    return configured.encode("utf-8")


def sign_proxy_target(stream_id: str, url: str) -> str:
    payload = json.dumps(
        {"s": str(stream_id), "u": str(url)},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(_proxy_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"{encoded}.{signature}"


def verify_proxy_target(value: str) -> dict:
    try:
        encoded, signature = value.rsplit(".", 1)
        expected = hmac.new(_proxy_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
        if not payload.get("s") or not _supported_stream_url(payload.get("u")):
            raise ValueError("invalid payload")
        return payload
    except Exception as exc:
        raise ValueError("Invalid IPTV proxy target") from exc


def build_iptv_streams(channel: dict, token: str) -> List[dict]:
    streams = []
    for index, source in enumerate(channel.get("streams") or [], start=1):
        url = str(source.get("url") or "")
        if not _supported_stream_url(url):
            continue
        quality = str(source.get("quality") or source.get("format") or "Live")
        feed_name = source.get("feed_name") or source.get("title") or ""
        languages = ", ".join(source.get("languages") or [])
        details = [channel.get("name") or "Live TV", quality]
        if feed_name:
            details.append(str(feed_name))
        if languages:
            details.append(languages)

        behavior_hints = {
            "notWebReady": True,
            "bingeGroup": f"telegram-stremio-iptv-{channel.get('_id')}",
        }
        request_headers = source.get("request_headers") or {}
        if request_headers:
            behavior_hints["proxyHeaders"] = {"request": request_headers}

        direct = {
            "name": f"Live TV {quality}",
            "title": "\n".join(details),
            "url": url,
            "behaviorHints": behavior_hints,
        }
        streams.append(direct)

        if request_headers and Telegram.IPTV_PROXY_FALLBACK_ENABLED:
            streams.append(
                {
                    "name": f"Live TV {quality} (VPS fallback)",
                    "title": "\n".join(details + ["Uses VPS bandwidth"]),
                    "url": f"{Telegram.BASE_URL}/iptv/{token}/stream/{source.get('id')}",
                    "behaviorHints": {
                        "notWebReady": True,
                        "bingeGroup": f"telegram-stremio-iptv-{channel.get('_id')}",
                    },
                }
            )
    return streams
