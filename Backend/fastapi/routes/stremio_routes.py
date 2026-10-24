import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, unquote

import PTN
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
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
from Backend.fastapi.themes import DEFAULT_THEME, get_theme
from Backend.helper.fanart import fanart_artwork
from Backend.helper.global_search import global_search, is_global_search_enabled
from Backend.helper.metadata.providers.cinemeta import get_detail, get_season
from Backend.helper.metadata import resolve_cover_url, COMBINED_SEASON, COMBINED_EPISODE_BASE, tmdb_api_key
from Backend.helper.database import convert_objectid_to_str
from Backend.helper.split_files import parse_combined_episodes, combined_name_key
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.stream_formatter import format_stream
from Backend.helper.subtitles import get_subtitles_for, stremio_subtitle_entries
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, get_streambot_url

router = APIRouter(prefix="/stremio", tags=["Stremio Addon"])

#----- Dedupe cache for the "Solicitar contenido" stream prompt:
# Android TV retries the stream GET every ~15s during playback failure, which
# would fire the n8n webhook + Telegram msg repeatedly. Cache (token, media_id)
# for 120s to ensure only ONE request/notification per Stremio stream-click.
_request_stream_cache: dict[tuple, float] = {}
_REQUEST_STREAM_TTL = 120  # seconds
templates = Jinja2Templates(directory="Backend/fastapi/templates")

#----- Addon configuration
ADDON_NAME = "Telegram"
ADDON_VERSION = __version__
PAGE_SIZE = 15


#----- Wrap a direct stream URL with the configured proxy (plain prepend or MediaFlow)
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

_membership_cache: dict = {}
_MEMBERSHIP_TTL = 60
_MEMBERSHIP_CACHE_MAX = 5000


#----- Drop cached membership results for one user or all users
def invalidate_membership_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _membership_cache.clear()
        return
    for key in [k for k in _membership_cache if k[1] == user_id]:
        _membership_cache.pop(key, None)


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
    if SettingsManager.current().subscription:
        return not token_data.get("subscription_expired")
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


#----- Resolution tag shown on the stream button (short label with sigla)
_RES_TAG_MAP = [
    (re.compile(r"2160p|4k|uhd", re.IGNORECASE), "4K (2160p)"),
    (re.compile(r"1080p|fhd", re.IGNORECASE), "FHD (1080p)"),
    (re.compile(r"720p|\bhd\b", re.IGNORECASE), "HD (720p)"),
    (re.compile(r"480p|\bsd\b", re.IGNORECASE), "SD (480p)"),
    (re.compile(r"360p", re.IGNORECASE), "360p"),
]

#----- Language / audio-track tags detected from the filename (order = display priority)
_LANG_TAG_MAP = [
    (re.compile(r"\b(lat(?:ino)?)\b", re.IGNORECASE), "Latino"),
    (re.compile(r"\b(cast(?:ellano)?|espa[nñ]ol|esp)\b", re.IGNORECASE), "Español"),
    (re.compile(r"\b(dual(?:[\s._-]*audio)?)\b", re.IGNORECASE), "Dual"),
    (re.compile(r"\b(multi(?:[\s._-]*audio)?)\b", re.IGNORECASE), "Multi"),
    (re.compile(r"\b(ingl[ée]s|english|eng)\b", re.IGNORECASE), "Inglés"),
]

#----- Known source/rip tags (kept short — no release group / site branding)
_SOURCE_TAG_MAP = [
    (re.compile(r"web[\s._-]?dl", re.IGNORECASE), "WEB-DL"),
    (re.compile(r"web[\s._-]?rip", re.IGNORECASE), "WEBRip"),
    (re.compile(r"blu[\s._-]?ray|bd[\s._-]?rip", re.IGNORECASE), "BluRay"),
    (re.compile(r"hd[\s._-]?rip", re.IGNORECASE), "HDRip"),
    (re.compile(r"hdtv", re.IGNORECASE), "HDTV"),
    (re.compile(r"\bdvd\b", re.IGNORECASE), "DVD"),
    (re.compile(r"\bcam\b", re.IGNORECASE), "CAM"),
]


def _resolution_tag(filename: str, quality: str) -> str:
    for rx, label in _RES_TAG_MAP:
        if rx.search(filename) or (quality and rx.search(str(quality))):
            return label
    return str(quality) if quality else "HD"


def _detect_language_tags(filename: str) -> str:
    found = []
    for rx, label in _LANG_TAG_MAP:
        if rx.search(filename) and label not in found:
            found.append(label)
    return " + ".join(found)


def _detect_source_tag(filename: str) -> str:
    for rx, label in _SOURCE_TAG_MAP:
        if rx.search(filename):
            return label
    return ""


#----- Build a Stremio stream display name/title from a filename.
#----- Deliberately never exposes the raw filename (site/release-group branding) —
#----- only clean tags: resolution, source, codec, language, size.
def format_stream_details(filename: str, quality: str, size: str, is_split: bool = False) -> tuple[str, str]:
    size_emoji = "📦" if is_split else "💾"
    filename = filename or ""

    try:
        parsed = PTN.parse(filename)
    except Exception:
        parsed = {}

    res_tag = _resolution_tag(filename, quality)
    stream_name = f"🎬 {res_tag}"

    source_tag = _detect_source_tag(filename)
    codec = parsed.get("codec") or ""
    top_line_parts = [p for p in (source_tag, codec) if p]
    top_line = " · ".join(top_line_parts)

    lang_tag = _detect_language_tags(filename)

    title_lines = []
    if top_line:
        title_lines.append(f"🌐 {top_line}")
    if lang_tag:
        title_lines.append(f"🔊 {lang_tag}")
    title_lines.append(f"{size_emoji} {size}")

    stream_title = "\n".join(title_lines)
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

    #----- Show expiry info in the addon: token's own expiry first, else the subscription
    try:
        expiry_obj = token_data.get("expires_at")
        if expiry_obj is None and SettingsManager.current().subscription:
            user_id = token_data.get("user_id")
            if user_id:
                user = await db.get_user(int(user_id))
                if user and user.get("subscription_status") == "active":
                    expiry_obj = user.get("subscription_expiry")

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


def _streams_from_global_results(token: str, global_results: list) -> list:
    streams = []
    # Never expose the source channel name or the original message caption to
    # the end user — show only a neutral "Telegram" source label so private
    # channel / chat names (e.g. "ya pi, dejame vivir") never leak into Stremio.
    for r in global_results:
        is_split = bool(r.get("is_split"))
        # Pass the quality as the "filename" so format_stream_details only ever
        # derives clean tags (resolution/source/lang) and never echoes the raw
        # caption text back to the UI.
        _, stream_title = format_stream_details(r.get("quality") or "", r["quality"], r["size"], is_split=is_split)
        stream_name = sort_name = f"🌐 GLOBAL {r['quality']}"
        stream_title = f"{stream_title}\n📡 Telegram"
        if is_split:
            kind = "zip parts" if r.get("is_zip") else "parts"
            stream_title += f" · 📦 {r.get('part_count', 0)} {kind}"
        # Use a neutral slug in the download URL instead of the raw caption.
        url = f"{SettingsManager.current().base_url}/dl/{token}/{r['token']}/{quote(r.get('quality') or 'video')}"
        size_bytes = parse_size_to_bytes(r.get("size", ""))
        custom = format_stream(SettingsManager.current(), r["title"], size_bytes, addon_name="🌐 Global",
                               is_split=is_split, parts=r.get("part_count", 0), channel=r.get("source_chat"))
        if custom:
            stream_name, stream_title = custom
        url = f"{SettingsManager.current().base_url}/dl/{token}/{r['token']}/{quote(r['title'])}"
        if cf_enabled():
            cf_url = cf_stream_url(token, r['token'], quote(r['title']))
            if SettingsManager.current().cf_stream_mode == "cloudflare":
                url = cf_url
            else:
                streams.append({"name": f"{stream_name} (Cloudflare)", "title": stream_title, "url": cf_url, "size_bytes": size_bytes, "sort_name": sort_name,
                                "behaviorHints": {"bingeGroup": _binge_group(f"global-{r['quality']}", "(Cloudflare)")}})
        streams.append({"name": stream_name, "title": stream_title, "url": url, "size_bytes": size_bytes, "sort_name": sort_name,
                        "behaviorHints": {"bingeGroup": _binge_group(f"global-{r['quality']}")}})
    return streams


async def _global_streams_for(
    token: str,
    imdb_id: str = None,
    media_type: str = "series",
    season_num: Optional[int] = None,
    episode_num: Optional[int] = None,
    kitsu_id: Optional[int] = None,
    absolute_episode: Optional[int] = None,
    is_anime: bool = False,
) -> list:
    expected_title = ""
    year = None
    cinemeta_videos = []

    if kitsu_id is not None:
        expected_title, year = await _kitsu_title_year(int(kitsu_id))
        is_anime = True
    elif imdb_id:
        imdb_media_type = "tvSeries" if media_type == "series" else "movie"
        detail = await get_detail(imdb_id=imdb_id, media_type=imdb_media_type)
        if not detail or not detail.get("title"):
            return []
        expected_title = detail["title"]
        year = (detail.get("releaseDetailed") or {}).get("year") or None
        cinemeta_videos = detail.get("videos") or []
        if season_num is not None and episode_num is not None and absolute_episode is None:
            try:
                await get_season(imdb_id=imdb_id, season_id=season_num, episode_id=episode_num)
            except Exception:
                pass

    if not expected_title:
        return []

    search_season = season_num
    search_episode = episode_num
    if is_anime and absolute_episode is not None:
        search_season = None
        search_episode = absolute_episode
    elif is_anime and season_num is None and episode_num is not None:
        search_season = None
        search_episode = episode_num

    abs_ep = absolute_episode
    map_source = None
    if (
        abs_ep is None
        and media_type == "series"
        and imdb_id
        and season_num is not None
        and episode_num is not None
    ):
        try:
            from Backend.helper.metadata.episode_maps import absolute_from_imdb_episode

            mapped = await absolute_from_imdb_episode(
                imdb_id,
                int(season_num),
                int(episode_num),
                title=expected_title,
                videos=cinemeta_videos,
            )
            if mapped and mapped.get("is_anime"):
                is_anime = True
                map_source = mapped.get("source")
                abs_ep = mapped.get("absolute_episode")
                if abs_ep is None and int(season_num) == 1:
                    abs_ep = int(episode_num)
        except Exception as e:
            LOGGER.warning(f"[GLOBAL SEARCH] anime map lookup failed for {imdb_id}: {e}")

    mapped_from_sxx = (
        abs_ep is not None
        and absolute_episode is None
        and season_num is not None
        and episode_num is not None
    )
    if mapped_from_sxx:
        search_season = None
        search_episode = int(abs_ep)
        LOGGER.info(
            f"[GLOBAL SEARCH] Anime mapped S{int(season_num):02d}E{int(episode_num):02d} "
            f"→ absolute {int(abs_ep)} for '{expected_title}'"
            + (f" (via {map_source})" if map_source else "")
            + "; trying absolute first"
        )

    auth_channels = SettingsManager.current().auth_channels
    try:
        global_results = await global_search(
            expected_title,
            auth_channels,
            year=year,
            season=search_season,
            episode=search_episode,
        )
    except Exception as e:
        LOGGER.error(f"[GLOBAL SEARCH] search failed for '{expected_title}': {e}")
        global_results = []

    if not global_results and mapped_from_sxx:
        LOGGER.info(
            f"[GLOBAL SEARCH] Absolute {int(abs_ep)} empty for '{expected_title}'; "
            f"falling back to S{int(season_num):02d}E{int(episode_num):02d}"
        )
        try:
            global_results = await global_search(
                expected_title,
                auth_channels,
                year=year,
                season=season_num,
                episode=episode_num,
            )
        except Exception as e:
            LOGGER.error(f"[GLOBAL SEARCH] SxxExx fallback failed for '{expected_title}': {e}")
            global_results = []

    return _streams_from_global_results(token, global_results)


#----- Cached check that a user is still in the subscription group (fail-open)
async def _is_subscription_member(user_id: int) -> bool:
    group_id = SettingsManager.current().subscription_group_id
    if not group_id:
        return True

    cache_key = (group_id, user_id)
    cached = _membership_cache.get(cache_key)
    now_ts = time.monotonic()
    if cached and (now_ts - cached[0]) < _MEMBERSHIP_TTL:
        return cached[1]

    try:
        member = await StreamBot.get_chat_member(group_id, user_id)
        result = member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except UserNotParticipant:
        result = False
    except Exception as e:
        LOGGER.warning(f"[SUBSCRIPTION] Membership check failed for user {user_id}: {e}")
        return True

    if len(_membership_cache) >= _MEMBERSHIP_CACHE_MAX:
        for k in [k for k, v in _membership_cache.items() if (now_ts - v[0]) >= _MEMBERSHIP_TTL]:
            _membership_cache.pop(k, None)
        if len(_membership_cache) >= _MEMBERSHIP_CACHE_MAX:
            _membership_cache.clear()

    _membership_cache[cache_key] = (now_ts, result)
    return result


#----- Resolve playable streams for a title (Telegram library or Global Search)
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

    if token_data.get("subscription_expired"):
        return {
            "streams": [
                {
                    "name": "🚫 Plan Expired",
                    "title": "Your plan is expired.\nRenew it from the bot to continue watching.",
                    "url": get_streambot_url()
                }
            ]
        }

    #----- Subscription users must currently be members of the configured group.
    #----- Admin, lifetime and admin-set token-expiry grants skip this check.
    if (SettingsManager.current().subscription
            and not token_data.get("is_admin")
            and not token_data.get("subscription_exempt")
            and not token_data.get("expires_at")):
        user_id = token_data.get("user_id")
        if user_id and not await _is_subscription_member(int(user_id)):
            return {
                "streams": [
                    {
                        "name": "📢 Join Required",
                        "title": "First join the channel to stream it.\nThen wait for 2 min for verification",
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

    is_combined = season_num == COMBINED_SEASON and episode_num is not None and episode_num >= COMBINED_EPISODE_BASE

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

                combined = parse_combined_episodes(filename) if is_combined else None
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
    elif is_global_search_enabled():
        try:
            is_anime = bool(is_kitsu or (media_details and media_details.get("is_anime")))
            log_id = f"kitsu:{kitsu_id}" if is_kitsu else imdb_id
            LOGGER.info(f"{log_id}:{season_num}:{episode_num}:abs={absolute_episode}|{media_type}")
            streams.extend(
                await _global_streams_for(
                    token,
                    imdb_id=imdb_id,
                    media_type=media_type,
                    season_num=season_num,
                    episode_num=episode_num,
                    kitsu_id=kitsu_id,
                    absolute_episode=absolute_episode,
                    is_anime=is_anime,
                )
            )
        except Exception as e:
            LOGGER.error(f"[GLOBAL SEARCH] stream search failed for {id}: {e}")

    #----- Per-token quality filter (fall back to all if it would hide everything)
    config = token_data.get("config") or {}
    quality_filter = set(config.get("quality_filter") or [])
    if quality_filter and streams:
        filtered = [s for s in streams if stream_res_label(s.get("sort_name", s.get("name", ""))) in quality_filter]
        if filtered:
            streams = filtered

    if not streams:
        # No streams available — offer the user a one-click "request this title"
        # action. The imdb_id is stable and identifies the title across seasons.
        # NOTE: Stremio follows `url` as a video fetch, so we point it at a tiny
        # HTML redirect page that fires the request POST and then refreshes to /requests.
        # Use the FULL Stremio id (imdb_id:season:episode) so the webhook payload
        # carries the exact season/episode the user selected.
        _uid = id  # NOT imdb_id — that drops season/episode for series
        _redirect_url = f"{SettingsManager.current().base_url}/stremio/{token}/request-stream/{str(_uid)}?from=stremio"
        return {
            "streams": [
                {
                    "name": "📢 Solicitar contenido",
                    "title": "📩 No hay streams disponibles todavía. Hacé clic para solicitarlo — te avisamos cuando esté listo.",
                    "url": _redirect_url,
                    "behaviorHints": {"notWebReady": True},  # tells Stremio NOT to attempt media playback
                }
            ]
        }

    ascending = config.get("quality_sort") == "asc"
    if is_combined:
        streams.sort(key=lambda s: s.get("episode_start", 0))
        streams.sort(key=lambda s: s.get("name_key", ""))
        streams.sort(key=lambda s: get_resolution_priority(s.get("sort_name", s.get("name", ""))), reverse=not ascending)
    else:
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

#----- Stream a "solicitar contenido" click from the Stremio player back into
# the request pipeline (same webhook that the public /requests page uses).
# Returns a tiny HTML page with a meta-refresh so Stremio (which follows the
# stream `url`) does NOT attempt media playback — it loads this page, the JS
# fires the request via queue_stream_request, then redirects to /requests.
@router.get("/{token}/request-stream/{media_id}")
async def request_stream(
    token: str,
    media_id: str,
    request: Request,
    from_query: str = None,
):
    token_data = await db.get_api_token(token)
    if not token_data:
        return HTMLResponse(
            "<html><body><p>❌ Token inválido o expirado.</p>"
            "<script>window.location='/requests'</script></body></html>",
            status_code=404
        )
    base = SettingsManager.current().base_url
    referer = request.headers.get("referer") or base
    #----- Endpoint-level dedupe: Android TV retries the stream GET every ~15s
    # during playback failure. 120s TTL → only ONE webhook+telegram per click.
    cache_key = (token, media_id)
    now = time.monotonic()
    if cache_key in _request_stream_cache and (now - _request_stream_cache[cache_key]) < _REQUEST_STREAM_TTL:
        LOGGER.info(f"request_stream deduped (cached {now - _request_stream_cache[cache_key]:.1f}s ago): {media_id}")
        return HTMLResponse(
            "<html><head><meta http-equiv=\"refresh\" content=\"0;url=/requests?submitted=1\"></head>"
            "<body><p>📩 Solicitando contenido…</p></body></html>",
        )
    _request_stream_cache[cache_key] = now  # record this fire

    try:
        from Backend.helper.request_notifier import queue_stream_request
        await queue_stream_request(media_id, token_data, referer)
        # Return HTML page: server-side POST already fired above. Stremio's
        # WebView follows the stream `url` and tries to render it as media;
        # the JS fetch fires a companion call to _fire-request to ensure the
        # webhook POST completes server-side. meta-refresh then redirects to /requests.
        html = (
            '<html><head>\n'
            '<meta http-equiv="refresh" content="0;url=/requests?submitted=1">\n'
            '<script>\n'
            f'fetch("/stremio/{token}/_fire-request/{media_id}")\\n'
            '  .then(r => r.json())\n'
            '  .then(d => console.log("request", d))\n'
            '  .catch(e => console.error(e));\n'
            '</script>\n'
            '</head><body><p>📩 Solicitando contenido…</p></body></html>'
        )
        return HTMLResponse(html)
    except Exception as e:
        LOGGER.error(f"stream request failed for {media_id}: {e}")
        return HTMLResponse(
            "<html><body><p>❌ No se pudo solicitar el contenido.</p>"
            "<script>window.location='/requests'</script></body></html>",
            status_code=500,
        )

# Companion endpoint: the JS in the HTML above calls this to trigger the POST,
# keeping the request pipeline call server-side (no CORS issues from Stremio's view).
@router.get("/{token}/_fire-request/{media_id}")
async def fire_request(token: str, media_id: str, request: Request):
    token_data = await db.get_api_token(token)
    if not token_data:
        return JSONResponse({"error": "invalid token"}, status_code=404)
    base = SettingsManager.current().base_url
    referer = request.headers.get("referer") or base
    from Backend.helper.request_notifier import queue_stream_request
    result = await queue_stream_request(media_id, token_data, referer)
    return JSONResponse({"result": result})


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
        elif token_doc.get("subscription_exempt"):
            status_color, status_text, expiry_str = "#22c55e", "Active", "Never"
        elif token_expiry is not None:
            expiry_str = _fmt(token_expiry)
            if _expired(token_expiry):
                status_color, status_text = "#ef4444", "Expired"
            else:
                status_color, status_text = "#22c55e", "Active"
        elif SettingsManager.current().subscription:
            expiry = user.get("subscription_expiry") if user else None
            if user and user.get("subscription_status") == "active" and expiry and not _expired(expiry):
                status_color, status_text, expiry_str = "#22c55e", "Active", _fmt(expiry)
            else:
                status_color, status_text = "#ef4444", "Expired"
                expiry_str = _fmt(expiry) if expiry else "N/A"
        else:
            status_color, status_text, expiry_str = "#22c55e", "Active", "Never"

    return templates.TemplateResponse("stremio_configure.html", {
        "request": request,
        "theme": get_theme(request.session.get("theme", DEFAULT_THEME)),
        "manifest_url": manifest_url,
        "web_install_url": web_install_url,
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
