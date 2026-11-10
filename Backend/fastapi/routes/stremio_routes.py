import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, unquote

import PTN
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant

import httpx

from Backend import __version__, db
from Backend.helper.database import META_PROJECTION
from Backend.config import Telegram
from Backend.helper.analytics import client_ip_from, record_client
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.cf_stream import cf_enabled, cf_stream_url
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_theme
from Backend.helper.fanart import fanart_artwork
from Backend.helper.metadata.providers.cinemeta import get_detail, get_season
from Backend.helper.metadata import resolve_cover_url, tmdb_api_key
from Backend.helper.database import convert_objectid_to_str
from Backend.helper.split_files import parse_combined_episodes, combined_name_key
from Backend.helper.stream_badges import badge_lines
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.stream_formatter import format_stream
from Backend.helper.subtitles import get_subtitles_for, stremio_subtitle_entries
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, get_streambot_url

router = APIRouter(prefix="/stremio", tags=["Stremio Addon"])
templates = Jinja2Templates(directory="Backend/fastapi/templates")

#----- Addon configuration
ADDON_NAME = "Telegram"
ADDON_VERSION = __version__
PAGE_SIZE = 15

def _no_stream():
    text = "No stream found, ask admin to upload"
    return {"name": "Telegram", "title": text, "description": text}

def build_proxy_url(original_url: str) -> str | None:
    settings = SettingsManager.current()
    base = settings.http_proxy_url
    if not base:
        return None
    if settings.mediaflow_proxy:
        url = f"{base.rstrip('/')}/proxy/stream?d={quote(original_url, safe='')}"
        if settings.mediaflow_password:
            url += f"&api_password={quote(settings.mediaflow_password, safe='')}"
        return url
    return f"{base}{original_url}"

#----- Effective (mode, allowed_tokens) for a title, honouring per-item overrides
def _effective_visibility(catalog: dict, item: dict) -> tuple:
    if item.get("visibility") in ("public", "tokens", "owner"):
        return item["visibility"], (item.get("allowed_tokens") or [])
    return (catalog.get("visibility") or "public"), (catalog.get("allowed_tokens") or [])


#----- Whether a token may see content with the given visibility
def _token_can_view(mode: str, allowed_tokens: list, token_data: Optional[dict] = None) -> bool:
    if not token_data:
        return mode == "public"
    user_id = token_data.get("user_id")
    try:
        if user_id is not None and int(user_id) == int(Telegram.OWNER_ID):
            return True
    except (TypeError, ValueError):
        pass
    if mode == "owner":
        return False
    if mode == "tokens":
        return token_data.get("token") in (allowed_tokens or [])
    return True


#----- Mongo filter that hides owner-only / restricted titles from a token
def _visibility_query(token_data: Optional[dict] = None) -> dict:
    if not token_data:
        return {"$or": [
            {"visibility": {"$exists": False}},
            {"visibility": "public"},
        ]}
    user_id = token_data.get("user_id")
    try:
        if user_id is not None and int(user_id) == int(Telegram.OWNER_ID):
            return {}
    except (TypeError, ValueError):
        pass
    return {"$or": [
        {"visibility": {"$exists": False}},
        {"visibility": "public"},
        {"visibility": "tokens", "allowed_tokens": token_data.get("token")},
    ]}


#----- Hide titles locked to a single catalog from default listings / search
def _not_exclusive_clause(allow_searchable: bool = False) -> dict:
    ors = [{"exclusive_catalog_id": {"$exists": False}}, {"exclusive_catalog_id": None}]
    if allow_searchable:
        ors.append({"exclusive_searchable": True})
    return {"$or": ors}


#----- Combine non-empty Mongo filters under a single $and
def _merge_filters(*filters) -> dict:
    parts = [f for f in filters if f]
    if not parts:
        return {}
    return parts[0] if len(parts) == 1 else {"$and": parts}


_TMDB_TRENDING_CACHE: dict = {}
_TMDB_TRENDING_LOCKS: dict = {}
_TMDB_TRENDING_TTL = 600  # 10 minutes cache


async def _get_tmdb_trending_ids(media_type: str, time_window: str = "day") -> list[int]:
    """Fetch daily trending TMDB IDs (cached for 10 minutes)."""
    api_key = tmdb_api_key()
    if not api_key:
        return []

    cache_key = (media_type, time_window)
    now = time.monotonic()
    cached = _TMDB_TRENDING_CACHE.get(cache_key)
    if cached and now - cached[0] < _TMDB_TRENDING_TTL:
        return list(cached[1])

    lock = _TMDB_TRENDING_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _TMDB_TRENDING_CACHE.get(cache_key)
        if cached and now - cached[0] < _TMDB_TRENDING_TTL:
            return list(cached[1])

        ids: list[int] = []
        try:
            url = f"https://api.themoviedb.org/3/trending/{media_type}/{time_window}"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"api_key": api_key, "page": 1})
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("results") or []:
                        raw_id = item.get("id")
                        if raw_id:
                            try:
                                ids.append(int(raw_id))
                            except (TypeError, ValueError):
                                pass
        except Exception as exc:
            LOGGER.warning(f"[TMDB Trending] Fetch failed for {media_type}: {exc}")
            if cached:
                return list(cached[1])
            return []

        if ids:
            _TMDB_TRENDING_CACHE[cache_key] = (time.monotonic(), ids)
        return ids


async def _get_db_matches_in_tmdb_order(
    media_type: str, tmdb_ids: list[int], token_data: dict, genre_filter: Optional[str] = None
) -> list[dict]:
    """Match local MongoDB records for TMDB IDs while preserving exact TMDB trending rank."""
    if not tmdb_ids:
        return []

    collection_name = "tv" if media_type == "series" else "movie"
    query_parts = [
        {"tmdb_id": {"$in": tmdb_ids}},
        _visibility_query(token_data),
        _not_exclusive_clause(),
    ]
    if genre_filter:
        query_parts.append({"genres": {"$in": [genre_filter]}})
    query = _merge_filters(*query_parts)

    by_tmdb_id: dict[int, dict] = {}
    storage_keys = [key for key in db.dbs if str(key).startswith("storage_")]
    storage_keys.sort(key=lambda k: int(str(k).split("_", 1)[1]) if str(k).split("_", 1)[1].isdigit() else 0, reverse=True)

    for db_key in storage_keys:
        try:
            cursor = db.dbs[db_key][collection_name].find(query, META_PROJECTION)
            async for item in cursor:
                try:
                    t_id = int(item.get("tmdb_id"))
                except (TypeError, ValueError):
                    continue
                if t_id in by_tmdb_id:
                    continue
                if not _token_can_view(
                    item.get("visibility") or "public",
                    item.get("allowed_tokens") or [],
                    token_data,
                ):
                    continue
                by_tmdb_id[t_id] = convert_objectid_to_str(item)
        except Exception as exc:
            LOGGER.warning(f"[TMDB Trending] DB error querying {db_key}: {exc}")

    # Preserve the exact ranking from TMDB
    ordered = [by_tmdb_id[tid] for tid in tmdb_ids if tid in by_tmdb_id]
    return ordered


def _parse_stremio_id(id: str):
    parts = id.split(":")
    is_kitsu = parts and parts[0].lower() == "kitsu"
    imdb_id = None
    kitsu_id = None
    season_num = None
    episode_num = None
    absolute_episode = None

    if is_kitsu:
        try:
            kitsu_id = int(parts[1]) if len(parts) > 1 else None
        except (TypeError, ValueError):
            kitsu_id = None
        if len(parts) == 3:
            try:
                absolute_episode = int(parts[2])
            except (TypeError, ValueError):
                absolute_episode = None
            episode_num = absolute_episode
        elif len(parts) >= 4:
            try:
                season_num = int(parts[2]) if parts[2] not in ("", "null", "None") else None
            except (TypeError, ValueError):
                season_num = None
            try:
                episode_num = int(parts[3]) if parts[3] not in ("", "null", "None") else None
            except (TypeError, ValueError):
                episode_num = None
            if season_num is None and episode_num is not None:
                absolute_episode = episode_num
    else:
        imdb_id = parts[0] if parts else id
        try:
            season_num = int(parts[1]) if len(parts) > 1 and parts[1] not in ("", "null", "None") else None
        except (TypeError, ValueError):
            season_num = None
        try:
            episode_num = int(parts[2]) if len(parts) > 2 and parts[2] not in ("", "null", "None") else None
        except (TypeError, ValueError):
            episode_num = None

    return {
        "imdb_id": imdb_id,
        "kitsu_id": kitsu_id,
        "season_num": season_num,
        "episode_num": episode_num,
        "absolute_episode": absolute_episode,
        "is_kitsu": is_kitsu,
    }


async def _title_allowed(imdb_id: str = None, token_data: dict = None, kitsu_id: int = None) -> bool:
    doc = await db.get_media_details(imdb_id=imdb_id, kitsu_id=kitsu_id, projection={"visibility": 1, "allowed_tokens": 1})
    if not doc:
        return True
    return _token_can_view(doc.get("visibility") or "public", doc.get("allowed_tokens") or [], token_data)


#----- Available catalog genres
GENRES = [
    "Action", "Adventure", "Animation", "Biography", "Comedy",
    "Crime", "Documentary", "Drama", "Family", "Fantasy",
    "History", "Horror", "Music", "Mystery", "Romance",
    "Sci-Fi", "Sport", "Thriller", "War", "Western"
]


#----- Turn a stored image reference into an absolute URL for Stremio clients.
#----- Rebinds gradient covers and app-served /thumb paths to the current hosts.
def _abs_media_url(value: str) -> str:
    value = resolve_cover_url(value)
    idx = value.find("/thumb/")
    return f"{SettingsManager.current().base_url}{value[idx:]}" if idx != -1 else value


BETTERPOSTER_DEFAULT = "https://btttr.cc/poster/imdb/poster-default/{imdb_id}.jpg"
RPDB_FREE = "https://api.ratingposterdb.com/t0-free-rpdb/imdb/poster-default/{imdb_id}.jpg"


def _poster_url(imdb_id: str, fallback: str, media_type: str = "movie") -> str:
    settings = SettingsManager.current()
    if imdb_id:
        if settings.spatial_poster_enabled and settings.spatial_poster:
            return settings.spatial_poster.replace("{type}", media_type).replace("{imdb_id}", str(imdb_id))
        if settings.better_poster_enabled:
            template = settings.better_poster or BETTERPOSTER_DEFAULT
            return template.replace("{imdb_id}", str(imdb_id))
        if settings.rpdb_enabled:
            key = settings.rpdb_api_key
            template = (
                f"https://api.ratingposterdb.com/{key}/imdb/poster-default/{{imdb_id}}.jpg"
                if key else RPDB_FREE
            )
            return template.replace("{imdb_id}", str(imdb_id))
    return _abs_media_url(fallback)


async def _apply_fanart(meta: dict, item: dict) -> None:
    if not SettingsManager.current().fanart_enabled:
        return
    try:
        art = await fanart_artwork(item.get("imdb_id"), item.get("tmdb_id"), item.get("media_type"))
    except Exception as e:
        LOGGER.warning(f"[FANART] artwork lookup failed for {item.get('imdb_id')}: {e}")
        return
    if art.get("poster"):
        meta["poster"] = art["poster"]
    if art.get("logo"):
        meta["logo"] = art["logo"]
    if art.get("background"):
        meta["background"] = art["background"]



def _year_label(item: dict) -> str:
    """Single year or range for Stremio releaseInfo (e.g. 1999-2024)."""
    start = item.get("release_year")
    end = item.get("release_year_end")
    if not start:
        return ""
    try:
        start_i = int(start)
    except (TypeError, ValueError):
        return str(start)
    if end:
        try:
            end_i = int(end)
            if end_i > start_i:
                return f"{start_i}-{end_i}"
        except (TypeError, ValueError):
            pass
    return str(start_i)


#----- Map an internal media item into a Stremio meta object
def _display_title(item: dict) -> str:
    """Prefer English title when available, else canonical title."""
    eng = (item.get("title_english") or "").strip()
    title = (item.get("title") or "").strip()
    if eng and eng.lower() != title.lower():
        return eng
    return eng or title or "Unknown"


def _safe_moviedb_id(item: dict):
    """Never emit null/None for moviedb_id (breaks some Stremio clients)."""
    v = item.get("tmdb_id")
    if v is None or str(v).strip().lower() in ("", "null", "none"):
        return ""
    try:
        return int(v)
    except (TypeError, ValueError):
        return str(v)


def convert_to_stremio_meta(item: dict) -> dict:
    media_type = "series" if item.get("media_type") == "tv" else "movie"
    imdb = item.get("imdb_id") or ""
    meta = {
        "id": imdb,
        "type": media_type,
        "name": _display_title(item),
        "poster": _poster_url(imdb, item.get("poster"), media_type),
        "logo": item.get("logo") or "",
        "year": _year_label(item) or item.get("release_year") or "",
        "releaseInfo": _year_label(item) or "",
        "imdb_id": imdb,
        "moviedb_id": _safe_moviedb_id(item),
        "background": _abs_media_url(item.get("backdrop")),
        "genres": item.get("genres") or [],
        "imdbRating": str(item.get("rating") or "") if item.get("rating") not in (None, "") else "",
        "description": item.get("description") or "",
        "cast": item.get("cast") or [],
        "runtime": item.get("runtime") or "",
    }
    return meta


#----- Format a movie release date as an ISO string, or None
def format_released_date(media):
    year = media.get("release_year")
    if year:
        try:
            y = int(str(year)[:4])
            return datetime(y, 1, 1).isoformat() + "Z"
        except Exception:
            return None
    return None


#----- Build a Stremio stream display name/title from a filename
def format_stream_details(filename: str, quality: str, size: str, is_split: bool = False) -> tuple[str, str]:
    size_emoji = "📦" if is_split else "💾"
    try:
        parsed = PTN.parse(filename)
    except Exception:
        return (f"Telegram {quality}", f"📁 {filename}\n{size_emoji} {size}")

    codec_parts = []
    if parsed.get("codec"):
        codec_parts.append(f"🎥 {parsed.get('codec')}")
    if parsed.get("bitDepth"):
        codec_parts.append(f"🌈 {parsed.get('bitDepth')}bit")
    if parsed.get("audio"):
        codec_parts.append(f"🔊 {parsed.get('audio')}")
    if parsed.get("encoder"):
        codec_parts.append(f"👤 {parsed.get('encoder')}")

    codec_info = " ".join(codec_parts) if codec_parts else ""

    resolution = parsed.get("resolution", quality)
    quality_type = parsed.get("quality", "")
    stream_name = f"Telegram {resolution} {quality_type}".strip()

    stream_title_parts = [
        f"📁 {filename}",
        f"{size_emoji} {size}",
    ]
    #----- Text badges (same rules as the Prism image pack) replace the old codec line when they find anything
    quality_badges, language_badges = badge_lines(filename)
    if quality_badges:
        stream_title_parts.append(f"✦ {quality_badges}")
    elif codec_info:
        stream_title_parts.append(codec_info)
    if language_badges:
        stream_title_parts.append(f"🗣 {language_badges}")

    stream_title = "\n".join(stream_title_parts)
    return (stream_name, stream_title)


def parse_size_to_bytes(size_str: str) -> int:
    if not size_str:
        return 0
    match = re.match(r"([\d.]+)\s*([A-Za-z]+)", size_str.strip())
    if not match:
        return 0
    value, unit = float(match.group(1)), match.group(2).upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * multipliers.get(unit, 1))


def get_resolution_priority(stream_name: str) -> int:
    resolution_map = {
        "2160p": 2160, "4k": 2160, "uhd": 2160,
        "1080p": 1080, "fhd": 1080,
        "720p": 720, "hd": 720,
        "480p": 480, "sd": 480,
        "360p": 360,
    }
    for res_key, res_value in resolution_map.items():
        if res_key in stream_name.lower():
            return res_value
    return 1


#----- Canonical quality label used by per-token quality filtering
def stream_res_label(stream_name: str) -> str:
    return {2160: "4K", 1080: "1080p", 720: "720p", 480: "480p", 360: "360p"}.get(
        get_resolution_priority(stream_name), "other"
    )


#----- Manifest describing the addon's catalogs/resources for this token
@router.get("/{token}/manifest.json")
async def get_manifest(token: str, token_data: dict = Depends(verify_token)):
    if SettingsManager.current().hide_catalog:
        resources = ["stream", "subtitles"]
        catalogs = []
    else:
        resources = ["catalog", "meta", "stream", "subtitles"]
        catalogs = [
            {
                "type": "movie",
                "id": "latest_movies",
                "name": "Latest",
                "extra": [
                    {"name": "genre", "isRequired": False, "options": GENRES},
                    {"name": "skip"}
                ],
                "extraSupported": ["genre", "skip"]
            },
            {
                "type": "movie",
                "id": "top_movies",
                "name": "Popular",
                "extra": [
                    {"name": "genre", "isRequired": False, "options": GENRES},
                    {"name": "skip"},
                    {"name": "search", "isRequired": False}
                ],
                "extraSupported": ["genre", "skip", "search"]
            },
            {
                "type": "series",
                "id": "latest_series",
                "name": "Latest",
                "extra": [
                    {"name": "genre", "isRequired": False, "options": GENRES},
                    {"name": "skip"}
                ],
                "extraSupported": ["genre", "skip"]
            },
            {
                "type": "series",
                "id": "top_series",
                "name": "Popular",
                "extra": [
                    {"name": "genre", "isRequired": False, "options": GENRES},
                    {"name": "skip"},
                    {"name": "search", "isRequired": False}
                ],
                "extraSupported": ["genre", "skip", "search"]
            },
            {
                "type": "movie",
                "id": "trending_movies",
                "name": "Trending",
                "extra": [
                    {"name": "genre", "isRequired": False, "options": GENRES},
                    {"name": "skip"}
                ],
                "extraSupported": ["genre", "skip"]
            },
            {
                "type": "series",
                "id": "trending_series",
                "name": "Trending",
                "extra": [
                    {"name": "genre", "isRequired": False, "options": GENRES},
                    {"name": "skip"}
                ],
                "extraSupported": ["genre", "skip"]
            }
        ]

        try:
            custom_catalogs = await db.get_custom_catalogs()
            for catalog in custom_catalogs:
                visible_items = [
                    i for i in (catalog.get("items") or [])
                    if _token_can_view(*_effective_visibility(catalog, i), token_data)
                ]
                has_movie = any(i.get("media_type") == "movie" for i in visible_items)
                has_series = any(i.get("media_type") == "tv" for i in visible_items)
                if not has_movie and not has_series:
                    continue
                catalog_id = str(catalog.get("_id"))
                catalog_name = catalog.get("name") or "Custom Catalog"
                if has_movie:
                    catalogs.append({
                        "type": "movie",
                        "id": f"custom_{catalog_id}",
                        "name": catalog_name,
                        "extra": [{"name": "search", "isRequired": False}, {"name": "skip"}],
                        "extraSupported": ["search", "skip"],
                    })
                if has_series:
                    catalogs.append({
                        "type": "series",
                        "id": f"custom_{catalog_id}",
                        "name": catalog_name,
                        "extra": [{"name": "search", "isRequired": False}, {"name": "skip"}],
                        "extraSupported": ["search", "skip"],
                    })
        except Exception:
            pass

        try:
            order = await db.get_catalog_order()
            token_order = (token_data.get("config") or {}).get("catalog_order") or []
            effective = token_order or order
            if effective:
                rank = {k: i for i, k in enumerate(effective)}

                def _crank(c):
                    key = f"{c.get('id')}::{c.get('type')}"
                    return rank.get(key, rank.get(c.get("id"), len(effective) + 1))

                catalogs.sort(key=_crank)
            hidden = set((token_data.get("config") or {}).get("hidden_catalogs") or [])
            if hidden:
                catalogs = [
                    c for c in catalogs
                    if c.get("id") not in hidden and f"{c.get('id')}::{c.get('type')}" not in hidden
                ]
        except Exception:
            pass


    addon_name = ADDON_NAME
    addon_desc = "Streams movies and series from your Telegram."
    addon_version = ADDON_VERSION

    #----- Show the token's expiry in the addon
    try:
        expiry_obj = token_data.get("expires_at")
        if expiry_obj:
            expiry_str = expiry_obj.strftime("%d %b %Y").lstrip("0")
            addon_desc = (
                f"📅 Access active until {expiry_str}.\n"
                f"Streams movies and series from your Telegram."
            )
            epoch_tag = format(int(expiry_obj.timestamp()) & 0xFFFF, "x")
            addon_version = f"{ADDON_VERSION}-{epoch_tag}"
    except Exception:
        pass

    return {
        "id": f"telegram.media.{token[:8]}",
        "version": addon_version,
        "name": addon_name,
        "logo": "https://i.postimg.cc/XqWnmDXr/Picsart-25-10-09-08-09-45-867.png",
        "description": addon_desc,
        "types": ["movie", "series"],
        "resources": resources,
        "catalogs": catalogs,
        "idPrefixes": ["tt", "tg", "kitsu"],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False
        },
        "config": [
            {
                "key": "manifest_url",
                "title": "Your Addon URL (copy to reinstall)",
                "type": "text",
                "default": f"{SettingsManager.current().base_url}/stremio/{token}/manifest.json"
            }
        ]
    }


async def build_nuvio_collection_data(token: str, collection_type: str = "full", token_data: Optional[dict] = None) -> list:
    addon_id = f"telegram.media.{token[:8]}"
    
    if token_data is None and token:
        try:
            token_data = await db.get_api_token(token)
        except Exception:
            token_data = None

    # Map catalog names to catalog IDs for this token, separated by media type
    catalog_movies_map = {}
    catalog_series_map = {}
    auto_key_movies_map = {}
    auto_key_series_map = {}

    try:
        custom_catalogs = await db.get_custom_catalogs()
        for catalog in custom_catalogs:
            # Check visibility and items
            raw_items = catalog.get("items") or []
            visible_items = [
                i for i in raw_items
                if _token_can_view(*_effective_visibility(catalog, i), token_data)
            ]

            has_movie = any(i.get("media_type") == "movie" for i in visible_items)
            has_series = any(i.get("media_type") == "tv" for i in visible_items)

            cid = f"custom_{catalog.get('_id')}"
            name = (catalog.get("name") or "").lower().strip()
            auto_key = (catalog.get("auto_key") or "").lower().strip()

            if has_movie:
                catalog_movies_map[name] = cid
                if auto_key:
                    auto_key_movies_map[auto_key] = cid
            if has_series:
                catalog_series_map[name] = cid
                if auto_key:
                    auto_key_series_map[auto_key] = cid
    except Exception:
        pass

    # Helper to get catalog ID for movies and series only if they actually have content
    def get_cat_id(name_query: str, auto_key: str = "", media_type: str = "movie", fallback: Optional[str] = None, strict: bool = True):
        cmap = catalog_movies_map if media_type == "movie" else catalog_series_map
        amap = auto_key_movies_map if media_type == "movie" else auto_key_series_map
        if auto_key and auto_key.lower() in amap:
            return amap[auto_key.lower()]
        for name, cid in cmap.items():
            if name_query.lower() in name:
                return cid
        return None if strict else fallback

    hotstar_movie = get_cat_id("hotstar", "auto_hotstar", "movie", strict=True)
    hotstar_series = get_cat_id("hotstar", "auto_hotstar", "series", strict=True)
    netflix_movie = get_cat_id("netflix", "auto_netflix", "movie", strict=True)
    netflix_series = get_cat_id("netflix", "auto_netflix", "series", strict=True)
    prime_movie = get_cat_id("prime", "auto_prime_video", "movie", strict=True)
    prime_series = get_cat_id("prime", "auto_prime_video", "series", strict=True)
    apple_movie = get_cat_id("apple", "auto_apple_tv", "movie", strict=True)
    apple_series = get_cat_id("apple", "auto_apple_tv", "series", strict=True)
    hbo_movie = get_cat_id("hbo", "auto_hbo", "movie", strict=True)
    hbo_series = get_cat_id("hbo", "auto_hbo", "series", strict=True)
    hulu_movie = get_cat_id("hulu", "auto_hulu", "movie", strict=True)
    hulu_series = get_cat_id("hulu", "auto_hulu", "series", strict=True)
    mx_movie = get_cat_id("mx", "auto_mx_player", "movie", strict=True)
    mx_series = get_cat_id("mx", "auto_mx_player", "series", strict=True)
    sony_movie = get_cat_id("sony", "auto_sonyliv", "movie", strict=True)
    sony_series = get_cat_id("sony", "auto_sonyliv", "series", strict=True)
    zee5_movie = get_cat_id("zee", "auto_zee5", "movie", strict=True)
    zee5_series = get_cat_id("zee", "auto_zee5", "series", strict=True)
    crunchyroll_movie = get_cat_id("crunchyroll", "auto_crunchyroll", "movie", strict=True)
    crunchyroll_series = get_cat_id("crunchyroll", "auto_crunchyroll", "series", strict=True)
    bollywood_movie = get_cat_id("bollywood", "auto_bollywood", "movie", fallback="latest_movies", strict=False)
    bollywood_series = get_cat_id("bollywood", "auto_bollywood", "series", fallback="latest_series", strict=False)

    ott_collection = {
        "id": "collection-erfs5gwk-community",
        "title": "Streaming Services",
        "folders": [
          {
            "id": "68ba6673-3480-4601-980e-4f69862777c3",
            "title": "Trending Now",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": "trending_movies"},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": "trending_series"},
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": "latest_movies"},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": "latest_series"}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/2774e91b86.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/2774e91b86.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": "trending_movies"},
              {"type": "series", "addonId": addon_id, "catalogId": "trending_series"},
              {"type": "movie", "addonId": addon_id, "catalogId": "latest_movies"},
              {"type": "series", "addonId": addon_id, "catalogId": "latest_series"}
            ],
            "focusGifEnabled": True
          },
          {
            "id": "folder-8ZBZOMW9",
            "title": "JioHotstar",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": hotstar_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": hotstar_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/focused/disney-plus.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/disney-plus.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/04d275e49b.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": hotstar_movie},
              {"type": "series", "addonId": addon_id, "catalogId": hotstar_series}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/backdrop/disney-plus.webp"
          },
          {
            "id": "folder-WFGY1XPD",
            "title": "Netflix",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": netflix_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": netflix_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/59916efcd2.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/netflix.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/73a7509d9f.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": netflix_movie},
              {"type": "series", "addonId": addon_id, "catalogId": netflix_series}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/backdrop/netflix.webp"
          },
          {
            "id": "folder-AT8KYHZO",
            "title": "Amazon Prime Video",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": prime_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": prime_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/focused/prime-video.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/prime-video.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/cover/prime-video.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": prime_movie},
              {"type": "series", "addonId": addon_id, "catalogId": prime_series}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/backdrop/prime-video.webp"
          },
          {
            "id": "folder-IQTF27K8",
            "title": "Apple TV",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": apple_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": apple_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/291311c88b.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/apple-tv.webp",
            "coverImageUrl": "https://imkaptain.github.io/nuvio-assets/assets/images/4d8f0a8f.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": apple_movie},
              {"type": "series", "addonId": addon_id, "catalogId": apple_series}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/backdrop/apple-tv.webp"
          },
          {
            "id": "folder-OA4T8NYO",
            "title": "HBO Max",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": hbo_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": hbo_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/6b439cb84a.webp",
            "titleLogoUrl": "https://imkaptain.github.io/nuvio-assets/assets/images/6e4c10d3.webp",
            "coverImageUrl": "https://imkaptain.github.io/nuvio-assets/assets/images/b59babf1.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": hbo_movie},
              {"type": "series", "addonId": addon_id, "catalogId": hbo_series}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://raw.githubusercontent.com/bramst0ne/prism-wallpapers/main/collections/providers/1899-hbo-max/backdrops/t2_flat_1080p.jpg"
          },
          {
            "id": "folder-LN9SPMQN",
            "title": "Hulu",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": hulu_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": hulu_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/focused/hulu.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/hulu.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/cover/hulu.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": hulu_movie},
              {"type": "series", "addonId": addon_id, "catalogId": hulu_series}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/backdrop/hulu.webp"
          },
          {
            "id": "85a10640-79f2-435f-9d63-348e921e37f4",
            "title": "MX Player",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": mx_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": mx_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/739cc2d16c.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/9cfdcd25ba.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": mx_movie},
              {"type": "series", "addonId": addon_id, "catalogId": mx_series}
            ],
            "focusGifEnabled": True
          },
          {
            "id": "f6409d11-7630-4fa4-b6e6-48774e4df89e",
            "title": "Sony LIV",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": sony_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": sony_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/6b309267fc.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/cf8b51931f.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": sony_movie},
              {"type": "series", "addonId": addon_id, "catalogId": sony_series}
            ],
            "focusGifEnabled": True
          },
          {
            "id": "008c0c7c-cfe1-43f4-9630-8afd72b7cb5f",
            "title": "Zee5",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": zee5_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": zee5_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/364ce2abf4.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/364ce2abf4.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": zee5_movie},
              {"type": "series", "addonId": addon_id, "catalogId": zee5_series}
            ],
            "focusGifEnabled": True
          },
          {
            "id": "cf7a3d79-0cc9-42c5-b460-d2e3b838ecfc",
            "title": "Crunchyroll",
            "sources": [
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": crunchyroll_series},
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": crunchyroll_movie}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/faff458914.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/crunchyroll.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/f6456696da.webp",
            "catalogSources": [
              {"type": "series", "addonId": addon_id, "catalogId": crunchyroll_series},
              {"type": "movie", "addonId": addon_id, "catalogId": crunchyroll_movie}
            ],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/backdrop/crunchyroll.webp"
          }
        ],
        "pinToTop": True,
        "viewMode": "TABBED_GRID",
        "showAllTab": True,
        "focusGlowEnabled": True
    }

    # Auto-hide OTT folders whose catalogs are not enabled/synced in database
    cleaned_folders = []
    for f in ott_collection.get("folders") or []:
        sources = [s for s in f.get("sources") or [] if s.get("catalogId")]
        catalog_sources = [cs for cs in f.get("catalogSources") or [] if cs.get("catalogId")]
        if sources or f.get("id") == "68ba6673-3480-4601-980e-4f69862777c3":  # Keep Trending Now
            folder_copy = dict(f)
            folder_copy["sources"] = sources or f.get("sources")
            folder_copy["catalogSources"] = catalog_sources or f.get("catalogSources")
            cleaned_folders.append(folder_copy)
    cleaned_folders.sort(key=lambda f: f.get("id") == "68ba6673-3480-4601-980e-4f69862777c3")
    ott_collection["folders"] = cleaned_folders

    explore_collection = {
        "id": "966190a9-3785-4ef0-9f1b-883478bf618c-community",
        "title": "Explore & Discover🗺️",
        "folders": [
          {
            "id": "f2e9b8dc-f50a-4bc7-96d3-30053ce27ad6",
            "title": "India 🇮🇳",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": bollywood_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": bollywood_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/8d7391d9c2.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/e0a263395d.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": bollywood_movie},
              {"type": "series", "addonId": addon_id, "catalogId": bollywood_series}
            ],
            "focusGifEnabled": True
          },
          {
            "id": "5069a848-f76f-4c68-ba6b-142d86a24603",
            "title": "The Ultimate Collection",
            "sources": [
              {"title": "The Fast and the Furious", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 75},
              {"title": "Harry Potter", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 84},
              {"title": "Top Gun", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 5022174},
              {"title": "The James Bond (007)", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 1919354},
              {"title": "Jason Bourne", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 36572888},
              {"title": "Jurassic Park/World", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 29684902},
              {"title": "Mission: Impossible", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 131},
              {"title": "Indiana Jones", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 15},
              {"title": "Pirates of the Caribbean", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 10},
              {"title": "Middle-Earth", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 1642688},
              {"title": "Avatar", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 884},
              {"title": "John Wick", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 2435487},
              {"title": "Transformer", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 176},
              {"title": "The Matrix", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 81},
              {"title": "Planet of the Apes", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 11214053},
              {"title": "The MonsterVerse", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 25439284},
              {"title": "The Conjuring", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 3877179},
              {"title": "The Godfather", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 38},
              {"title": "The Rocky", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 11723321},
              {"title": "Alien & Predator", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 34030187},
              {"title": "Evil Dead", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 35297070},
              {"title": "The Terminator", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 36},
              {"title": "Die Hard", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 68},
              {"title": "Ip Man", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 11338691},
              {"title": "National Treasure", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 190},
              {"title": "Jaws", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 73},
              {"title": "Rambo", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 151},
              {"title": "Men in Black", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 10870886},
              {"title": "The Exorcist", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 416},
              {"title": "28 Days Later", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 36431272},
              {"title": "Bad Boys", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 11364118},
              {"title": "Ocean's", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 2702670},
              {"title": "The Hangover", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 854},
              {"title": "Home Alone", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 20735298},
              {"title": "Rush Hour", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 197}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/98803eb245.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/9c2a71d1f7.webp",
            "catalogSources": [],
            "focusGifEnabled": True
          },
          {
            "id": "74f27ef0-5e73-40e2-a189-63d951dbf792",
            "title": "Top Rated",
            "sources": [
              {"title": "IMDB: Top Rated Movies Movies", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 2142753},
              {"title": "IMDB: Top Rated TV Shows Series", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "TV", "traktListId": 2143363}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/7da625bb85.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/7da625bb85.webp",
            "catalogSources": [],
            "focusGifEnabled": True
          },
          {
            "id": "folder-516b0b3d",
            "title": "It's Aliens",
            "sources": [
              {"title": "It's Aliens", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 33753562}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/7098025963.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/9caca95ded.webp",
            "catalogSources": [],
            "focusGifEnabled": False
          },
          {
            "id": "cd74f767-b492-4a4c-a3a4-7296925e15a3",
            "title": "Marvel Cinematic Universe",
            "sources": [
              {"title": "Marvel Studios Movies", "sortBy": "primary_release_date.desc", "tmdbId": 420, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COMPANY"},
              {"title": "Marvel Studios Series", "sortBy": "primary_release_date.desc", "tmdbId": 420, "provider": "tmdb", "mediaType": "TV", "tmdbSourceType": "COMPANY"},
              {"title": "Marvel Animation Movies", "sortBy": "primary_release_date.desc", "tmdbId": 13252, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COMPANY"},
              {"title": "Marvel Animation Series", "sortBy": "primary_release_date.desc", "tmdbId": 13252, "provider": "tmdb", "mediaType": "TV", "tmdbSourceType": "COMPANY"},
              {"title": "X-Men (film series)", "sortBy": "original", "tmdbId": 27741, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "LIST"},
              {"title": "Spider Man Movies", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 36436842}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/5a9a039075.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/3b0c581d42.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/3a16c993c3.webp",
            "catalogSources": [],
            "focusGifEnabled": False,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/b1c930066e.webp"
          },
          {
            "id": "4975c018-a714-45d2-8079-49125d6a174b",
            "title": "DC Cinematic Universe",
            "sources": [
              {"title": "DC Extended Universe", "sortBy": "original", "tmdbId": 85568, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "LIST"},
              {"title": "DC Universe Animated Original Movies", "sortBy": "original", "tmdbId": 3255, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "LIST"},
              {"title": "Justice League Collection", "sortBy": "original", "tmdbId": 876125, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
              {"title": "Superman Collection", "sortBy": "original", "tmdbId": 1540907, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/761930c268.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/ce59bd8728.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/07e339efe0.webp",
            "catalogSources": [],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/07e339efe0.webp"
          },
          {
            "id": "72394705-9eaf-4d7d-8533-6b004f5287fe",
            "title": "Star Wars Universe",
            "sources": [
              {"title": "Star Wars Movies", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 22202556},
              {"title": "Star Wars: Shows & Series", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "TV", "traktListId": 22757470}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/7630f2e4da.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/ea85e73a82.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/db9c50b0bb.webp",
            "catalogSources": [],
            "focusGifEnabled": False,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/d2e20320bf.webp"
          },
          {
            "id": "65388509-423e-40f9-97cf-6a645474e35b",
            "title": "Walt Disney Pictures",
            "sources": [
              {"title": "Walt Disney Pictures", "sortBy": "popularity.desc", "tmdbId": 2, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COMPANY"}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/19d8cf3288.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/12d0a0fff2.webp",
            "catalogSources": [],
            "focusGifEnabled": True
          },
          {
            "id": "3b979d6d-2a1b-4cea-b7c7-a83d39ceb2ea",
            "title": "Pixar Studio",
            "sources": [
              {"title": "Pixar Feature Films", "sortBy": "released", "sortHow": "desc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 801240},
              {"title": "Pixar Short Films", "sortBy": "released", "sortHow": "desc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 2749609}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/cc4d00d1ac.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/a74590c7b2.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/0ee9b1188e.webp",
            "catalogSources": [],
            "focusGifEnabled": False,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/9b2710bf03.webp"
          },
          {
            "id": "5b8452e4-50c1-4097-8848-08057442438e",
            "title": "Dreamworks Studio",
            "sources": [
              {"title": "Dreamworks Feature Films", "sortBy": "released", "sortHow": "desc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 801242}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/02f99db3e1.webp",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/fd29838f20.webp",
            "coverImageUrl": "https://i.postimg.cc/JnVNH8M5/882cf7a8-9aa6-4673-81c1-d3ed7a859e95.jpg",
            "catalogSources": [],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/1d4d792e5f.webp"
          },
          {
            "id": "c2206436-c704-4fca-a026-f4d426c40992",
            "title": "Warner Bros. Pictures",
            "sources": [
              {"title": "Warner Bros.", "sortBy": "popularity.desc", "tmdbId": 174, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COMPANY"}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/d14037ad4b.webp",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/63987b738d.webp",
            "catalogSources": [],
            "focusGifEnabled": True
          },
          {
            "id": "folder-08ic2wbn",
            "title": "Anime",
            "sources": [
              {
                "id": "src-60QLR3CH",
                "name": "Trending Anime Series",
                "genre": "Trending Anime Series",
                "title": "Trending Anime Series",
                "sortBy": "popularity.desc",
                "filters": {
                  "withGenres": "16",
                  "voteCountGte": 60,
                  "withoutKeywords": "198385|378816|155477|256466|329280|570|1817|15078|13059|195669|325693|360629|284535|356759|337946",
                  "withOriginalLanguage": "ja"
                },
                "provider": "tmdb",
                "mediaType": "TV",
                "tmdbSourceType": "DISCOVER"
              },
              {
                "id": "src-3OBFNGID",
                "name": "Top Rated Anime Series",
                "genre": "Top Rated Anime Series",
                "title": "Top Rated Anime Series",
                "sortBy": "vote_average.desc",
                "filters": {
                  "withGenres": "16",
                  "voteCountGte": 400,
                  "withoutKeywords": "198385|378816|155477|256466|329280|570|1817|15078|13059|195669|325693|360629|284535|356759|337946",
                  "withOriginalLanguage": "ja"
                },
                "provider": "tmdb",
                "mediaType": "TV",
                "tmdbSourceType": "DISCOVER"
              },
              {
                "id": "src-28C4UZ10",
                "name": "Trending Anime Films",
                "genre": "Trending Anime Films",
                "title": "Trending Anime Films",
                "sortBy": "popularity.desc",
                "filters": {
                  "withGenres": "16",
                  "voteCountGte": 120,
                  "withoutKeywords": "198385|378816|155477|256466|329280|570|1817|15078|13059|195669|325693|360629|284535|356759|337946",
                  "withOriginalLanguage": "ja"
                },
                "provider": "tmdb",
                "mediaType": "MOVIE",
                "tmdbSourceType": "DISCOVER"
              },
              {
                "id": "src-BK4KJJNV",
                "name": "Top Rated Anime Films",
                "genre": "Top Rated Anime Films",
                "title": "Top Rated Anime Films",
                "sortBy": "vote_average.desc",
                "filters": {
                  "withGenres": "16",
                  "voteCountGte": 400,
                  "withoutKeywords": "198385|378816|155477|256466|329280|570|1817|15078|13059|195669|325693|360629|284535|356759|337946",
                  "withOriginalLanguage": "ja"
                },
                "provider": "tmdb",
                "mediaType": "MOVIE",
                "tmdbSourceType": "DISCOVER"
              },
              {
                "id": "src-ABWAIOIK",
                "name": "New This Season",
                "genre": "New This Season",
                "title": "New This Season",
                "sortBy": "first_air_date.desc",
                "filters": {
                  "withGenres": "16",
                  "voteCountGte": 5,
                  "releaseDateGte": (datetime.utcnow() - timedelta(days=120)).strftime("%Y-%m-%d"),
                  "withoutKeywords": "198385|378816|155477|256466|329280|570|1817|15078|13059|195669|325693|360629|284535|356759|337946",
                  "withOriginalLanguage": "ja"
                },
                "provider": "tmdb",
                "mediaType": "TV",
                "tmdbSourceType": "DISCOVER"
              },
              {
                "id": "src-MODCLS10",
                "name": "Modern Classics of the 2010s",
                "genre": "Modern Classics of the 2010s",
                "title": "Modern Classics of the 2010s",
                "sortBy": "vote_average.desc",
                "filters": {
                  "withGenres": "16",
                  "voteCountGte": 200,
                  "releaseDateGte": "2010-01-01",
                  "releaseDateLte": "2019-12-31",
                  "withoutKeywords": "198385|378816|155477|256466|329280|570|1817|15078|13059|195669|325693|360629|284535|356759|337946",
                  "withOriginalLanguage": "ja"
                },
                "provider": "tmdb",
                "mediaType": "TV",
                "tmdbSourceType": "DISCOVER"
              }
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/82cc501318.webp",
            "titleLogoUrl": "https://raw.githubusercontent.com/ImKaptain/nuvio-art/main/art/anime/discover-anime/discover-anime-logo.png",
            "coverImageUrl": "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio/19a76f435c.webp",
            "catalogSources": [],
            "focusGifEnabled": True,
            "heroBackdropUrl": "https://raw.githubusercontent.com/ImKaptain/nuvio-art/main/art/anime/discover-anime/discover-anime-backdrop.jpg"
          }
        ],
        "pinToTop": True,
        "viewMode": "TABBED_GRID",
        "showAllTab": True,
        "focusGlowEnabled": True
    }

    if collection_type == "ott":
        return [ott_collection]
    elif collection_type == "explore":
        return [explore_collection]
    return [ott_collection, explore_collection]


#----- Dynamic Nuvio Collection JSON for this token
@router.get("/{token}/nuvio-collection.json")
async def get_nuvio_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "full", token_data)


@router.get("/{token}/nuvio-ott-collection.json")
async def get_nuvio_ott_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "ott", token_data)


@router.get("/{token}/nuvio-explore-collection.json")
async def get_nuvio_explore_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "explore", token_data)


#----- Catalog listing (latest/popular/custom, with genre/search/skip)
@router.get("/{token}/catalog/{media_type}/{id}/{extra:path}.json")
@router.get("/{token}/catalog/{media_type}/{id}.json")
async def get_catalog(token: str, media_type: str, id: str, extra: Optional[str] = None, token_data: dict = Depends(verify_token)):
    if SettingsManager.current().hide_catalog:
        raise HTTPException(status_code=404, detail="Catalog disabled")

    if media_type not in ["movie", "series"]:
        raise HTTPException(status_code=404, detail="Invalid catalog type")

    genre_filter = None
    search_query = None
    stremio_skip = 0

    if extra:
        params = extra.replace("&", "/").split("/")
        for param in params:
            if param.startswith("genre="):
                genre_filter = unquote(param.removeprefix("genre="))
            elif param.startswith("search="):
                search_query = unquote(param.removeprefix("search="))
            elif param.startswith("skip="):
                try:
                    stremio_skip = int(param.removeprefix("skip="))
                except ValueError:
                    stremio_skip = 0

    page = (stremio_skip // PAGE_SIZE) + 1

    try:
        if id.startswith("custom_") or id.startswith("auto_"):
            catalog_id = id.removeprefix("custom_")
            catalog = await db.get_custom_catalog(catalog_id)
            if not catalog:
                return {"metas": []}

            db_media_type = "tv" if media_type == "series" else "movie"
            visible_items = [
                it for it in (catalog.get("items") or [])
                if it.get("media_type") == db_media_type
                and _token_can_view(*_effective_visibility(catalog, it), token_data)
            ]
            visible_items.sort(key=lambda it: it.get("updated_on") or it.get("added_at") or datetime.min, reverse=True)
            if search_query:
                ids = [int(it["tmdb_id"]) for it in visible_items if it.get("tmdb_id") is not None]
                found = await db.search_documents(
                    query=search_query, page=page, page_size=PAGE_SIZE,
                    extra_filter=_merge_filters({"tmdb_id": {"$in": ids}}, _visibility_query(token_data)),
                    media_type=db_media_type,
                )
                items = [it for it in found.get("results", []) if it.get("media_type") == db_media_type]
            else:
                start = (page - 1) * PAGE_SIZE
                items = await db.get_documents(visible_items[start:start + PAGE_SIZE])
                items = [it for it in items if _token_can_view(it.get("visibility") or "public", it.get("allowed_tokens") or [], token_data)]
        elif search_query:
            search_results = await db.search_documents(
                query=search_query, page=page, page_size=PAGE_SIZE,
                extra_filter=_merge_filters(_visibility_query(token_data), _not_exclusive_clause(allow_searchable=True)),
                media_type="tv" if media_type == "series" else "movie",
            )
            all_items = search_results.get("results", [])
            db_media_type = "tv" if media_type == "series" else "movie"
            items = [item for item in all_items if item.get("media_type") == db_media_type]
        elif id in {"trending_movies", "trending_series"}:
            expected_type = "movie" if id == "trending_movies" else "series"
            if media_type != expected_type:
                return {"metas": []}

            items = []
            tmdb_type = "movie" if media_type == "movie" else "tv"
            trending_ids = await _get_tmdb_trending_ids(tmdb_type, "day")
            if trending_ids:
                db_matches = await _get_db_matches_in_tmdb_order(
                    media_type, trending_ids, token_data, genre_filter=genre_filter
                )
                if search_query:
                    q = search_query.lower()
                    db_matches = [
                        it for it in db_matches
                        if q in (it.get("title") or "").lower()
                        or q in (it.get("name") or "").lower()
                        or q in (it.get("original_title") or "").lower()
                    ]
                start = max(stremio_skip, 0)
                items = db_matches[start:start + PAGE_SIZE]

            # Fallback to latest uploaded media if TMDB is unconfigured, empty, or returns no DB matches
            if not items and stremio_skip == 0:
                LOGGER.info(f"[TMDB Trending] Falling back to latest {media_type} for catalog {id}")
                sort_params = [("updated_on", "desc")]
                vis_filter = _merge_filters(_visibility_query(token_data), _not_exclusive_clause())
                if media_type == "movie":
                    data = await db.sort_movies(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, extra_filter=vis_filter, projection=META_PROJECTION)
                    items = data.get("movies", [])
                else:
                    data = await db.sort_tv_shows(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, extra_filter=vis_filter, projection=META_PROJECTION)
                    items = data.get("tv_shows", [])
        else:
            if "latest" in id:
                sort_params = [("updated_on", "desc")]
            elif "top" in id:
                sort_params = [("rating", "desc")]
            else:
                sort_params = [("updated_on", "desc")]

            vis_filter = _merge_filters(_visibility_query(token_data), _not_exclusive_clause())
            if media_type == "movie":
                data = await db.sort_movies(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, extra_filter=vis_filter, projection=META_PROJECTION)
                items = data.get("movies", [])
            else:
                data = await db.sort_tv_shows(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, extra_filter=vis_filter, projection=META_PROJECTION)
                items = data.get("tv_shows", [])
    except Exception:
        return {"metas": []}

    metas = [convert_to_stremio_meta(item) for item in items]
    if SettingsManager.current().fanart_enabled:
        await asyncio.gather(*(_apply_fanart(m, it) for m, it in zip(metas, items)))
    return {"metas": metas}


@router.get("/{token}/meta/{media_type}/{id}.json")
async def get_meta(token: str, media_type: str, id: str, token_data: dict = Depends(verify_token)):
    if SettingsManager.current().hide_catalog:
        raise HTTPException(status_code=404, detail="Catalog disabled")

    parsed = _parse_stremio_id(id)
    imdb_id = parsed["imdb_id"] if not parsed["is_kitsu"] else None
    kitsu_id = parsed["kitsu_id"]

    media = await db.get_media_details(
        imdb_id=imdb_id, kitsu_id=kitsu_id, projection={"telegram": 0, "seasons.episodes.telegram": 0}
    )
    if not media:
        return {"meta": {}}

    if not _token_can_view(media.get("visibility") or "public", media.get("allowed_tokens") or [], token_data):
        return {"meta": {}}

    meta_id = id
    if parsed["is_kitsu"] and kitsu_id is not None:
        meta_id = f"kitsu:{kitsu_id}"
    elif media.get("imdb_id"):
        meta_id = media.get("imdb_id")

    meta_obj = {
        "id": meta_id,
        "type": "series" if media.get("media_type") == "tv" else "movie",
        "name": _display_title(media),
        "description": media.get("description") or "",
        "year": _year_label(media) or (str(media.get("release_year")) if media.get("release_year") else ""),
        "imdbRating": str(media.get("rating") or "") if media.get("rating") not in (None, "") else "",
        "genres": media.get("genres") or [],
        "poster": _poster_url(media.get("imdb_id") or imdb_id, media.get("poster"), "series" if media.get("media_type") == "tv" else "movie"),
        "logo": media.get("logo") or "",
        "background": _abs_media_url(media.get("backdrop")),
        "imdb_id": media.get("imdb_id") or (id if not parsed["is_kitsu"] else ""),
        "releaseInfo": _year_label(media) or "",
        "moviedb_id": _safe_moviedb_id(media),
        "cast": media.get("cast") or [],
        "runtime": media.get("runtime") or "",
    }

    await _apply_fanart(meta_obj, media)

    if media.get("media_type") == "movie":
        released_date = format_released_date(media)
        if released_date:
            meta_obj["released"] = released_date

    if media_type == "series":
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        videos = []
        seasons = media.get("seasons") or []

        def _snum(s):
            try:
                return int(s.get("season_number"))
            except (TypeError, ValueError):
                return 0

        def _enum(e):
            try:
                return int(e.get("episode_number"))
            except (TypeError, ValueError):
                return 0

        for season in sorted(seasons, key=_snum):
            s_num = _snum(season)
            episodes = season.get("episodes") or []
            for episode in sorted(episodes, key=_enum):
                e_num = _enum(episode)
                if not episodes:
                    continue
                abs_ep = episode.get("absolute_episode")
                if parsed["is_kitsu"] and kitsu_id is not None:
                    if abs_ep is not None:
                        episode_id = f"kitsu:{kitsu_id}:{abs_ep}"
                    else:
                        episode_id = f"kitsu:{kitsu_id}:{s_num}:{e_num}"
                else:
                    episode_id = f"{meta_id}:{s_num}:{e_num}"
                ep_title = (
                    episode.get("title")
                    or episode.get("episode_title")
                    or (f"E{e_num:02d}" if s_num != 0 else f"Combined {e_num}")
                )
                videos.append({
                    "id": episode_id,
                    "title": ep_title,
                    "season": s_num,
                    "episode": e_num,
                    "overview": (
                        episode.get("overview")
                        or episode.get("episode_overview")
                        or "No description available for this episode yet."
                    ),
                    "released": episode.get("released") or episode.get("episode_released") or yesterday,
                    "thumbnail": _abs_media_url(
                        episode.get("episode_backdrop") or episode.get("thumbnail")
                    ) or "https://raw.githubusercontent.com/weebzone/Colab-Tools/refs/heads/main/no_episode_backdrop.png",
                    "imdb_id": episode.get("imdb_id") or media.get("imdb_id") or (id if not parsed["is_kitsu"] else ""),
                })
        meta_obj["videos"] = videos
        if not videos:
            LOGGER.warning(f"[META] series {id} has no episode entries in DB")
    return {"meta": meta_obj}


#----- Subtitles for a title/episode, sourced from subtitle files in the channels
@router.get("/{token}/subtitles/{media_type}/{id}/{extra:path}.json")
@router.get("/{token}/subtitles/{media_type}/{id}.json")
async def get_subtitles(token: str, media_type: str, id: str, extra: Optional[str] = None, token_data: dict = Depends(verify_token)):
    try:
        parts = id.split(":")
        imdb_id = parts[0]
        season = int(parts[1]) if len(parts) > 1 else None
        episode = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        return {"subtitles": []}

    db_media_type = "tv" if media_type == "series" else "movie"
    subs = await get_subtitles_for(imdb_id, db_media_type, season, episode)
    if not subs:
        return {"subtitles": []}
    return {"subtitles": stremio_subtitle_entries(subs, token, SettingsManager.current().base_url)}


async def _kitsu_title_year(kitsu_id: int) -> tuple:
    try:
        from Backend.helper.metadata.providers.kitsu import get_anizip_mappings, _get_client, KITSU_URL
        client = await _get_client()
        resp = await client.get(f"{KITSU_URL}/anime/{int(kitsu_id)}")
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        attrs = data.get("attributes") or {}
        titles = attrs.get("titles") or {}
        title = (
            titles.get("en")
            or titles.get("en_jp")
            or attrs.get("canonicalTitle")
            or titles.get("ja_jp")
            or ""
        )
        year = None
        start = attrs.get("startDate") or ""
        if start and len(str(start)) >= 4:
            try:
                year = int(str(start)[:4])
            except (TypeError, ValueError):
                year = None
        if not title:
            doc = await get_anizip_mappings(int(kitsu_id)) or {}
            title = (doc.get("title") or {}).get("en") or (doc.get("title") or {}).get("x-jat") or ""
        return title, year
    except Exception as e:
        LOGGER.warning(f"[KITSU] title lookup failed for {kitsu_id}: {e}")
        return "", None


#----- Autoplay-next keeps the same quality and route (Cloudflare/Proxy/Direct)
def _binge_group(quality: str, suffix: str = "") -> str:
    route = suffix.strip("() ").lower()
    return f"telegram-stremio-{quality}" + (f"-{route}" if route else "")


#----- Resolve playable streams for a title from the Telegram library
@router.get("/{token}/stream/{media_type}/{id}.json")
async def get_streams(
    token: str,
    media_type: str,
    id: str,
    request: Request,
    token_data: dict = Depends(verify_token)
):
    #----- Capture the real app/device from the addon-protocol UA (not the spoofed video UA)
    asyncio.create_task(record_client(
        token,
        token_data.get("name") if token_data else None,
        client_ip_from(request),
        request.headers.get("user-agent", ""),
    ))

    if token_data.get("token_expired"):
        return {
            "streams": [
                {
                    "name": "🚫 Link Expired",
                    "title": "Your access link has expired.\nAsk the admin for a new one.",
                    "url": get_streambot_url()
                }
            ]
        }

    if token_data.get("limit_exceeded"):
        limit_type = token_data["limit_exceeded"]

        title = (
            "🚫 Daily Limit Reached – Upgrade Required"
            if limit_type == "daily"
            else "🚫 Monthly Limit Reached – Upgrade Required"
        )

        return {
            "streams": [
                {
                    "name": "Limit Reached",
                    "title": title,
                    "url": f"tg://user?id={Telegram.OWNER_ID}"
                }
            ]
        }

    try:
        parsed = _parse_stremio_id(id)
        imdb_id = parsed["imdb_id"]
        kitsu_id = parsed["kitsu_id"]
        season_num = parsed["season_num"]
        episode_num = parsed["episode_num"]
        absolute_episode = parsed["absolute_episode"]
        is_kitsu = parsed["is_kitsu"]
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid Stremio ID format")

    if is_kitsu and kitsu_id is None:
        raise HTTPException(status_code=400, detail="Invalid Kitsu ID format")

    if not await _title_allowed(imdb_id=imdb_id, token_data=token_data, kitsu_id=kitsu_id):
        return {"streams": []}

    media_details = await db.get_media_details(
        imdb_id=imdb_id,
        season_number=season_num,
        episode_number=episode_num,
        kitsu_id=kitsu_id,
        absolute_episode=absolute_episode,
    )

    streams = []


    if media_details and "telegram" in media_details:
        fmt_meta = {
            "queryType": media_type, "type": media_type, "isAnime": bool(media_details.get("is_anime")),
            "title": media_details.get("title"), "year": media_details.get("release_year"),
            "season": season_num, "episode": episode_num,
        }
        for quality in media_details.get("telegram", []):
            if quality.get("id"):
                filename = quality.get("name", "")
                quality_str = quality.get("quality", "HD")
                size = quality.get("size", "")
                size_bytes = parse_size_to_bytes(size)

                combined = parse_combined_episodes(filename)
                episode_start = combined.get("start") or 0 if combined else 0
                name_key = combined_name_key(filename) if combined else ""

                stream_name, stream_title = format_stream_details(
                    filename, quality_str, size, is_split=bool(quality.get("group_key"))
                )

                sort_name = stream_name
                original_url = f"{SettingsManager.current().base_url}/dl/{token}/{quality.get('id')}/video.mkv"
                proxy_url = build_proxy_url(original_url)
                cf_url = cf_stream_url(token, quality.get('id'), "video.mkv") if cf_enabled() else None
                cf_only = cf_url and SettingsManager.current().cf_stream_mode == "cloudflare"

                def add(suffix, url, proxied=False):
                    name, title = stream_name, stream_title
                    custom = format_stream(
                        SettingsManager.current(), filename, size_bytes, addon_name=ADDON_NAME, proxied=proxied,
                        is_split=bool(quality.get("group_key")), meta=fmt_meta,
                    )
                    if custom:
                        name, title = custom
                    if combined:
                        label = "Full" if combined.get("start") is None else f"E{combined['start']:02d}-E{combined['end']:02d}"
                        if label.lower() not in name.lower():
                            name = f"{name} {label}"
                    streams.append({"name": f"{name} {suffix}".strip(), "title": title, "url": url, "size_bytes": size_bytes,
                                    "episode_start": episode_start, "name_key": name_key, "sort_name": sort_name,
                                    "behaviorHints": {"bingeGroup": _binge_group(quality_str, suffix)}})

                if cf_url:
                    add("" if cf_only else "(Cloudflare)", cf_url)
                if not cf_only:
                    if SettingsManager.current().show_proxy_and_non_proxy_both and proxy_url:
                        add("(Proxy)", proxy_url, True)
                        add("(Direct)", original_url)
                    else:
                        add("", proxy_url or original_url, bool(proxy_url))

    #----- Per-token quality filter (fall back to all if it would hide everything)
    config = token_data.get("config") or {}
    quality_filter = set(config.get("quality_filter") or [])
    if quality_filter and streams:
        filtered = [s for s in streams if stream_res_label(s.get("sort_name", s.get("name", ""))) in quality_filter]
        if filtered:
            streams = filtered

    if not streams:
        return {"streams": [_no_stream()]}

    ascending = config.get("quality_sort") == "asc"
    streams.sort(
        key=lambda s: (get_resolution_priority(s.get("sort_name", s.get("name", ""))), s.get("size_bytes", 0)),
        reverse=not ascending
    )
    name_count: dict = {}
    for s in streams:
        name_count[s["name"]] = name_count.get(s["name"], 0) + 1

    seen: dict = {}
    for s in streams:
        if name_count[s["name"]] > 1 and SettingsManager.current().stream_formatter == "default":
            seen[s["name"]] = seen.get(s["name"], 0) + 1
            s["name"] = f"{s['name']} ({seen[s['name']]})"
    return {"streams": streams}

#----- Configure/install landing page rendered as HTML for a token
@router.get("/{token}/configure")
async def configure_addon(token: str, request: Request):
    manifest_url = f"{SettingsManager.current().base_url}/stremio/{token}/manifest.json"
    web_install_url = f"https://web.stremio.com/#/?addon_manifest={quote(manifest_url, safe='')}"

    token_doc = await db.get_api_token(token)
    user_name = "Unknown"
    expiry_str = "Never"
    status_color = "#ef4444"
    status_text = "Unknown"

    def _expired(when):
        ref = datetime.utcnow()
        try:
            if when.tzinfo is not None:
                ref = datetime.now(timezone.utc)
        except AttributeError:
            pass
        return when < ref

    def _fmt(when):
        try:
            return when.strftime("%d %b %Y").lstrip("0")
        except Exception:
            return "N/A"

    if token_doc:
        uid = token_doc.get("user_id")
        is_admin = bool(token_doc.get("is_admin"))
        try:
            is_admin = is_admin or (uid is not None and int(uid) == int(Telegram.OWNER_ID))
        except (TypeError, ValueError):
            pass

        user = None
        if uid:
            try:
                user = await db.get_user(int(uid))
            except Exception:
                user = None
        if user:
            user_name = user.get("first_name") or user.get("username") or f"User {uid}"
        elif uid:
            user_name = f"User {uid}"

        token_expiry = token_doc.get("expires_at")
        if is_admin:
            status_color, status_text, expiry_str = "#22c55e", "Admin", "Never"
        elif token_expiry is not None:
            expiry_str = _fmt(token_expiry)
            if _expired(token_expiry):
                status_color, status_text = "#ef4444", "Expired"
            else:
                status_color, status_text = "#22c55e", "Active"
        else:
            status_color, status_text, expiry_str = "#22c55e", "Active", "Never"

    return templates.TemplateResponse("stremio_configure.html", {
        "request": request,
        "theme": get_theme(request.session.get("theme", DEFAULT_THEME), request.session.get("style", DEFAULT_STYLE)),
        "manifest_url": manifest_url,
        "web_install_url": web_install_url,
        "badge_url": f"{SettingsManager.current().base_url}/badges/prism.json",
        "user_name": user_name,
        "expiry_str": expiry_str,
        "status_text": status_text,
        "status_color": status_color,
    })


#----- Catalogs this token can see, in effective (token or global) order
async def _addon_catalogs_for_token(token_data: dict) -> list:
    entries = [
        {"id": "latest_movies", "name": "Latest Movies", "type": "movie"},
        {"id": "top_movies", "name": "Popular Movies", "type": "movie"},
        {"id": "latest_series", "name": "Latest Series", "type": "series"},
        {"id": "top_series", "name": "Popular Series", "type": "series"},
        {"id": "trending_movies", "name": "Trending Movies", "type": "movie"},
        {"id": "trending_series", "name": "Trending Series", "type": "series"},
    ]
    try:
        for c in await db.get_custom_catalogs():
            items = [i for i in (c.get("items") or []) if _token_can_view(*_effective_visibility(c, i), token_data)]
            if not items:
                continue
            cid, name = f"custom_{c['_id']}", (c.get("name") or "Catalog")
            if any(i.get("media_type") == "movie" for i in items):
                entries.append({"id": cid, "name": name, "type": "movie"})
            if any(i.get("media_type") == "tv" for i in items):
                entries.append({"id": cid, "name": name, "type": "series"})
    except Exception:
        pass
    for e in entries:
        e["key"] = f"{e['id']}::{e['type']}"
    order = await db.get_catalog_order()
    tconf = token_data.get("config") or {}
    effective = tconf.get("catalog_order") or order
    if effective:
        rank = {k: i for i, k in enumerate(effective)}
        entries.sort(key=lambda e: rank.get(e["key"], rank.get(e["id"], len(effective) + 1)))
    return entries


#----- Read this token's addon config + catalog list (public, used by configure page)
@router.get("/{token}/addon-config")
async def get_addon_config(token: str):
    doc = await db.get_api_token(token)
    if not doc:
        raise HTTPException(status_code=404, detail="Invalid token")
    return {"config": doc.get("config") or {}, "catalogs": await _addon_catalogs_for_token(doc)}


#----- Persist this token's addon config (public, used by configure page)
@router.post("/{token}/addon-config")
async def save_addon_config(token: str, payload: dict):
    doc = await db.get_api_token(token)
    if not doc:
        raise HTTPException(status_code=404, detail="Invalid token")
    valid_q = {"480p", "720p", "1080p", "4K"}
    config = {
        "quality_sort": "asc" if payload.get("quality_sort") == "asc" else "desc",
        "quality_filter": [q for q in (payload.get("quality_filter") or []) if q in valid_q],
        "hidden_catalogs": [str(x) for x in (payload.get("hidden_catalogs") or [])],
        "catalog_order": [str(x) for x in (payload.get("catalog_order") or [])],
    }
    await db.set_token_config(token, config)
    return {"ok": True, "config": config}
