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
from Backend.helper.global_search import global_search, is_global_search_enabled
from Backend.helper.metadata.providers.cinemeta import get_detail, get_season
from Backend.helper.metadata import resolve_cover_url, COMBINED_SEASON, COMBINED_EPISODE_BASE, tmdb_api_key, get_tmdb_client, format_tmdb_image
from Backend.helper.database import convert_objectid_to_str
from Backend.helper.split_files import parse_combined_episodes, combined_name_key
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

def _donation():
    text = "Enjoying Telegram Stremio?\nTap to support the developer"
    return {"name": "❤️ Support", "title": text, "description": text, "externalUrl": "https://donate.weebzonex.workers.dev"}

async def _external_meta_for_id(id: str, parsed: dict, media_type: str) -> Optional[dict]:
    """Resolve basic metadata from TMDB or Cinemeta when an item is not in the database.
    This prevents Stremio/Nuvio from crashing with 'Could not load details' on un-cached items."""
    try:
        tmdb_id = parsed.get("tmdb_id")
        if tmdb_id is not None and tmdb_api_key():
            client = get_tmdb_client()
            is_movie = media_type == "movie" or parsed.get("tmdb_media_type") == "movie"
            target = client.movie(tmdb_id) if is_movie else client.tv(tmdb_id)
            item = await target.details(append_to_response="images,credits,external_ids")
            if not item:
                return None

            name = getattr(item, "title", None) if is_movie else getattr(item, "name", None)
            name = name or getattr(item, "original_title", None) or getattr(item, "original_name", None) or ""

            rel_date = getattr(item, "release_date", None) if is_movie else getattr(item, "first_air_date", None)
            year_str = str(rel_date.year) if (rel_date and getattr(rel_date, "year", None)) else ""

            ext_ids = getattr(item, "external_ids", None)
            external_imdb = getattr(ext_ids, "imdb_id", None) if ext_ids else None

            images = getattr(item, "images", None)
            logo_url = ""
            if images and getattr(images, "logos", None):
                for l in images.logos:
                    if getattr(l, "iso_639_1", None) == "en" and getattr(l, "file_path", None):
                        logo_url = format_tmdb_image(l.file_path, "w300")
                        break
                if not logo_url and images.logos and getattr(images.logos[0], "file_path", None):
                    logo_url = format_tmdb_image(images.logos[0].file_path, "w300")

            genres = [getattr(g, "name", "") for g in (getattr(item, "genres", None) or []) if getattr(g, "name", None)]
            runtime = getattr(item, "runtime", "") if is_movie else ""
            if not runtime and not is_movie:
                r_list = getattr(item, "episode_run_time", None) or []
                runtime = str(r_list[0]) if r_list else ""

            credits_data = getattr(item, "credits", None)
            cast_list = []
            if credits_data and getattr(credits_data, "cast", None):
                cast_list = [getattr(c, "name", "") for c in credits_data.cast[:10] if getattr(c, "name", None)]

            meta_id = id
            videos = []
            if not is_movie:
                seasons = getattr(item, "seasons", None) or []
                yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                for s in seasons:
                    s_num = getattr(s, "season_number", 0)
                    if s_num == 0:
                        continue
                    ep_count = getattr(s, "episode_count", 0) or 0
                    for ep in range(1, ep_count + 1):
                        videos.append({
                            "id": f"{meta_id}:{s_num}:{ep}",
                            "title": f"Episode {ep}",
                            "season": s_num,
                            "episode": ep,
                            "released": yesterday,
                        })

            return {
                "id": meta_id,
                "type": "movie" if is_movie else "series",
                "name": name,
                "description": getattr(item, "overview", "") or "",
                "year": year_str,
                "releaseInfo": year_str,
                "poster": format_tmdb_image(getattr(item, "poster_path", None), "w500"),
                "background": format_tmdb_image(getattr(item, "backdrop_path", None), "original"),
                "logo": logo_url,
                "genres": genres,
                "imdbRating": str(round(getattr(item, "vote_average", 0), 1)) if getattr(item, "vote_average", None) else "",
                "imdb_id": external_imdb or "",
                "moviedb_id": tmdb_id,
                "cast": cast_list,
                "runtime": str(runtime) if runtime else "",
                "videos": videos,
            }

        imdb_id = parsed.get("imdb_id")
        if imdb_id and imdb_id.startswith("tt"):
            external = await get_detail(imdb_id, "movie" if media_type == "movie" else "series")
            if external and external.get("title"):
                release = external.get("releaseDetailed") or {}
                year = release.get("year") or ""
                return {
                    "id": imdb_id,
                    "type": "movie" if media_type == "movie" else "series",
                    "name": external.get("title") or "",
                    "description": external.get("plot") or "",
                    "year": str(year) if year else "",
                    "releaseInfo": str(year) if year else "",
                    "poster": external.get("poster") or "",
                    "background": external.get("background") or "",
                    "logo": external.get("logo") or "",
                    "genres": external.get("genre") or [],
                    "imdbRating": str((external.get("rating") or {}).get("star") or ""),
                    "imdb_id": imdb_id,
                    "moviedb_id": external.get("moviedb_id") or "",
                    "cast": external.get("cast") or [],
                    "runtime": external.get("runtime") or "",
                    "videos": external.get("videos") or [],
                }
    except Exception as exc:
        LOGGER.warning(f"[EXTERNAL META] Failed to resolve fallback meta for {id}: {exc}")
    return None

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
    prefix = parts[0].lower() if parts else ""
    is_kitsu = prefix == "kitsu"
    is_tmdb = prefix == "tmdb"
    imdb_id = None
    kitsu_id = None
    tmdb_id = None
    tmdb_media_type = None
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
    elif is_tmdb:
        # Supports tmdb:movie:<id>, tmdb:tv:<id>, tmdb:<id>, and tmdb:<id>:<season>:<ep>
        id_index = 1
        if len(parts) > 2 and parts[1].lower() in ("movie", "tv", "series"):
            tmdb_media_type = "movie" if parts[1].lower() == "movie" else "tv"
            id_index = 2
        try:
            tmdb_id = int(parts[id_index]) if len(parts) > id_index else None
        except (TypeError, ValueError):
            tmdb_id = None
        trailing = parts[id_index + 1:]
        if len(trailing) >= 2:
            try:
                season_num = int(trailing[0]) if trailing[0] not in ("", "null", "None") else None
            except (TypeError, ValueError):
                season_num = None
            try:
                episode_num = int(trailing[1]) if trailing[1] not in ("", "null", "None") else None
            except (TypeError, ValueError):
                episode_num = None
        elif len(trailing) == 1:
            try:
                episode_num = int(trailing[0])
            except (TypeError, ValueError):
                episode_num = None
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
        "tmdb_id": tmdb_id,
        "tmdb_media_type": tmdb_media_type,
        "season_num": season_num,
        "episode_num": episode_num,
        "absolute_episode": absolute_episode,
        "is_kitsu": is_kitsu,
        "is_tmdb": is_tmdb,
    }


async def _title_allowed(imdb_id: str = None, token_data: dict = None, kitsu_id: int = None, tmdb_id: int = None, media_type: str = None) -> bool:
    doc = await db.get_media_details(imdb_id=imdb_id, kitsu_id=kitsu_id, tmdb_id=tmdb_id, media_type=media_type, projection={"visibility": 1, "allowed_tokens": 1})
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
    if codec_info:
        stream_title_parts.append(codec_info)

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
            custom_catalogs = await db.get_custom_catalogs(slim=True)
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
        "idPrefixes": ["tt", "tg", "kitsu"] + (["tmdb"] if tmdb_api_key() else []),
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
        custom_catalogs = await db.get_custom_catalogs(slim=True)
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
    south_indian_movie = get_cat_id("south indian", "auto_south_indian", "movie", strict=True)
    south_indian_series = get_cat_id("south indian", "auto_south_indian", "series", strict=True)
    kdrama_movie = get_cat_id("korean", "auto_korean", "movie", strict=True)
    kdrama_series = get_cat_id("k-drama", "auto_kdrama", "series", strict=True) or get_cat_id("korean", "auto_korean", "series", strict=True)
    anime_movie = get_cat_id("anime", "auto_anime", "movie", strict=True)
    anime_series = get_cat_id("anime", "auto_anime", "series", strict=True)

    # =========================================================================
    # 01 ✦ DISCOVER COLLECTION (TMDB Global Trends + Local Addon Fallback)
    # =========================================================================
    K_IMG = "https://raw.githubusercontent.com/Kaoxt/The-Kollection/main/images"
    NUVIO_CDN = "https://cdn.jsdelivr.net/gh/weebzone/Telegram-Stremio@master/assets/nuvio"

    # =========================================================================
    # 01 ✦ DISCOVER COLLECTION (TMDB Global Trends + Local Addon Fallback)
    # =========================================================================
    discover_collection = {
        "id": "collection-discover-main",
        "title": "Discover",
        "folders": [
            {
                "id": "folder-discover-trending",
                "title": "Trending Now",
                "sources": [
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": "trending_movies"},
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": "trending_series"},
                    {"title": "Trending Movies", "sortBy": "popularity.desc", "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "DISCOVER"},
                    {"title": "Trending TV Series", "sortBy": "popularity.desc", "provider": "tmdb", "mediaType": "TV", "tmdbSourceType": "DISCOVER"}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Discover/Trending/logo.webp",
                "coverImageUrl": f"{K_IMG}/Discover/Trending/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Discover/Trending/backdrop.webp",
                "catalogSources": [
                    {"type": "movie", "addonId": addon_id, "catalogId": "trending_movies"},
                    {"type": "series", "addonId": addon_id, "catalogId": "trending_series"}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-discover-popular",
                "title": "Popular Hits",
                "sources": [
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": "top_movies"},
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": "top_series"},
                    {"title": "Popular Movies", "sortBy": "popularity.desc", "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "DISCOVER"},
                    {"title": "Popular Series", "sortBy": "popularity.desc", "provider": "tmdb", "mediaType": "TV", "tmdbSourceType": "DISCOVER"}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Discover/Popular/logo.webp",
                "coverImageUrl": f"{K_IMG}/Discover/Popular/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Discover/Popular/backdrop.webp",
                "catalogSources": [
                    {"type": "movie", "addonId": addon_id, "catalogId": "top_movies"},
                    {"type": "series", "addonId": addon_id, "catalogId": "top_series"}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-discover-latest",
                "title": "Latest Releases",
                "sources": [
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": "latest_movies"},
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": "latest_series"},
                    {
                        "title": "Latest Released Movies",
                        "sortBy": "primary_release_date.desc",
                        "filters": {
                            "voteCountGte": 15,
                            "releaseDateGte": (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d"),
                            "releaseDateLte": datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        },
                        "provider": "tmdb",
                        "mediaType": "MOVIE",
                        "tmdbSourceType": "DISCOVER"
                    },
                    {
                        "title": "New TV Series Episodes",
                        "sortBy": "first_air_date.desc",
                        "filters": {
                            "voteCountGte": 10,
                            "releaseDateGte": (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d"),
                            "releaseDateLte": datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        },
                        "provider": "tmdb",
                        "mediaType": "TV",
                        "tmdbSourceType": "DISCOVER"
                    }
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Discover/Premieres/logo.webp",
                "coverImageUrl": f"{K_IMG}/Discover/Premieres/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Discover/Premieres/backdrop.webp",
                "catalogSources": [
                    {"type": "movie", "addonId": addon_id, "catalogId": "latest_movies"},
                    {"type": "series", "addonId": addon_id, "catalogId": "latest_series"}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-discover-toprated",
                "title": "Top Rated All-Time",
                "sources": [
                    {"title": "IMDb: Top Rated Movies", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 2142753},
                    {"title": "IMDb: Top Rated TV Shows", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "TV", "traktListId": 2143363},
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": "top_movies"},
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": "top_series"}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Discover/Top%20Rated/logo.webp",
                "coverImageUrl": f"{K_IMG}/Discover/Top%20Rated/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Discover/Top%20Rated/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            }
        ],
        "pinToTop": True,
        "viewMode": "TABBED_GRID",
        "showAllTab": True,
        "focusGlowEnabled": True
    }

    # =========================================================================
    # 02 ✦ STREAMING PLATFORMS (OTT Hubs)
    # =========================================================================
    ott_collection = {
        "id": "collection-erfs5gwk-community",
        "title": "Streaming Platforms",
        "folders": [
          {
            "id": "folder-WFGY1XPD",
            "title": "Netflix",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": netflix_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": netflix_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/Netflix/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/Netflix.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/Netflix.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": netflix_movie},
              {"type": "series", "addonId": addon_id, "catalogId": netflix_series}
            ],
            "focusGifEnabled": False
          },
          {
            "id": "folder-AT8KYHZO",
            "title": "Prime Video",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": prime_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": prime_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/Prime/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/PrimeVideo.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/PrimeVideo.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": prime_movie},
              {"type": "series", "addonId": addon_id, "catalogId": prime_series}
            ],
            "focusGifEnabled": False
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
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/Disney+/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/JioHotstar.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/JioHotstar.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": hotstar_movie},
              {"type": "series", "addonId": addon_id, "catalogId": hotstar_series}
            ],
            "focusGifEnabled": False
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
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/Apple%20TV/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/AppleTV.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/AppleTV.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": apple_movie},
              {"type": "series", "addonId": addon_id, "catalogId": apple_series}
            ],
            "focusGifEnabled": False
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
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/HBO%20Max/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/HBOMax.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/HBOMax.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": hbo_movie},
              {"type": "series", "addonId": addon_id, "catalogId": hbo_series}
            ],
            "focusGifEnabled": False
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
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/Hulu/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/hulu.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/hulu.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": hulu_movie},
              {"type": "series", "addonId": addon_id, "catalogId": hulu_series}
            ],
            "focusGifEnabled": False
          },
          {
            "id": "folder-ZEE5-OTT",
            "title": "ZEE5",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": zee5_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": zee5_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "",
            "titleLogoUrl": "https://cdn.jsdelivr.net/gh/luckynumb3rs/stremio-perfect-setup/collections/streaming/title/zee5.webp",
            "coverImageUrl": f"{NUVIO_CDN}/Zee5.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/Zee5.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": zee5_movie},
              {"type": "series", "addonId": addon_id, "catalogId": zee5_series}
            ],
            "focusGifEnabled": False
          },
          {
            "id": "folder-SONY-OTT",
            "title": "SonyLIV",
            "sources": [
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": sony_movie},
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": sony_series}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Studios/Sony%20Pictures/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/SonyLiv.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/SonyLiv.webp",
            "catalogSources": [
              {"type": "movie", "addonId": addon_id, "catalogId": sony_movie},
              {"type": "series", "addonId": addon_id, "catalogId": sony_series}
            ],
            "focusGifEnabled": False
          },
          {
            "id": "85a10640-79f2-435f-9d63-348e921e37f4",
            "title": "Crunchyroll",
            "sources": [
              {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": crunchyroll_series},
              {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": crunchyroll_movie}
            ],
            "hideTitle": False,
            "tileShape": "landscape",
            "focusGifUrl": "",
            "titleLogoUrl": f"{K_IMG}/Streaming%20Platforms/Crunchyroll/logo.webp",
            "coverImageUrl": f"{NUVIO_CDN}/Crunchyroll.webp",
            "heroBackdropUrl": f"{NUVIO_CDN}/Crunchyroll.webp",
            "catalogSources": [
              {"type": "series", "addonId": addon_id, "catalogId": crunchyroll_series},
              {"type": "movie", "addonId": addon_id, "catalogId": crunchyroll_movie}
            ],
            "focusGifEnabled": False
          }
        ],
        "pinToTop": True,
        "viewMode": "TABBED_GRID",
        "showAllTab": True,
        "focusGlowEnabled": True
    }

    # Sanitize OTT folders: keep only those with active sources and strip any null catalogIds
    cleaned_ott_folders = []
    for f in ott_collection.get("folders") or []:
        sources = [s for s in f.get("sources") or [] if s.get("catalogId")]
        catalog_sources = [cs for cs in f.get("catalogSources") or [] if cs.get("catalogId")]
        if sources:
            folder_copy = dict(f)
            folder_copy["sources"] = sources
            folder_copy["catalogSources"] = catalog_sources
            cleaned_ott_folders.append(folder_copy)
    ott_collection["folders"] = cleaned_ott_folders

    # =========================================================================
    # 03 ✦ FRANCHISES & UNIVERSES (Curated Sagas - Kaoxt The-Kollection)
    # =========================================================================
    franchises_collection = {
        "id": "collection-franchises-main",
        "title": "Franchises & Universes",
        "folders": [
            {
                "id": "folder-mcu-universe",
                "title": "Marvel Cinematic Universe",
                "sources": [
                    {"title": "Marvel Studios Movies", "sortBy": "primary_release_date.desc", "tmdbId": 420, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COMPANY"},
                    {"title": "Marvel Studios Series", "sortBy": "primary_release_date.desc", "tmdbId": 420, "provider": "tmdb", "mediaType": "TV", "tmdbSourceType": "COMPANY"},
                    {"title": "Spider-Man Films", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 36436842},
                    {"title": "X-Men Universe", "sortBy": "original", "tmdbId": 27741, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "LIST"}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Studios/Marvel/logo.webp",
                "coverImageUrl": f"{K_IMG}/Studios/Marvel/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Studios/Marvel/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            },
            {
                "id": "folder-dcu-universe",
                "title": "DC Universe & Batman",
                "sources": [
                    {"title": "DC Extended Universe", "sortBy": "original", "tmdbId": 85568, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "LIST"},
                    {"title": "Batman Collection", "sortBy": "original", "tmdbId": 263, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
                    {"title": "DC Animated Originals", "sortBy": "original", "tmdbId": 3255, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "LIST"}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Studios/DC/logo.webp",
                "coverImageUrl": f"{K_IMG}/Studios/DC/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Studios/DC/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            },
            {
                "id": "folder-starwars-universe",
                "title": "Star Wars Universe",
                "sources": [
                    {"title": "Star Wars Movies", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 22202556},
                    {"title": "Star Wars: Shows & Series", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "TV", "traktListId": 22757470}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Franchises/Star%20Wars/logo.webp",
                "coverImageUrl": f"{K_IMG}/Franchises/Star%20Wars/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Franchises/Star%20Wars/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            },
            {
                "id": "folder-james-bond-007",
                "title": "James Bond (007)",
                "sources": [
                    {"title": "The James Bond (007) Collection", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 1919354}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Franchises/007/logo.webp",
                "coverImageUrl": f"{K_IMG}/Franchises/007/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Franchises/007/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            },
            {
                "id": "folder-lotr-middleearth",
                "title": "Middle-Earth (LOTR & The Hobbit)",
                "sources": [
                    {"title": "The Lord of the Rings Collection", "sortBy": "original", "tmdbId": 119, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
                    {"title": "The Hobbit Collection", "sortBy": "original", "tmdbId": 121938, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
                    {"title": "Middle-Earth Saga", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 1642688}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Franchises/The%20Lord%20of%20the%20Rings/logo.webp",
                "coverImageUrl": f"{K_IMG}/Franchises/The%20Lord%20of%20the%20Rings/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Franchises/The%20Lord%20of%20the%20Rings/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            },
            {
                "id": "folder-harry-potter",
                "title": "Harry Potter & Wizarding World",
                "sources": [
                    {"title": "Harry Potter Collection", "sortBy": "original", "tmdbId": 1241, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
                    {"title": "Fantastic Beasts Collection", "sortBy": "original", "tmdbId": 435259, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
                    {"title": "Wizarding World", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 84}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Franchises/Harry%20Potter/logo.webp",
                "coverImageUrl": f"{K_IMG}/Franchises/Harry%20Potter/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Franchises/Harry%20Potter/backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            },
            {
                "id": "folder-ultimate-sagas",
                "title": "The Ultimate Sagas & Marathons",
                "sources": [
                    {"title": "Dune Collection", "sortBy": "original", "tmdbId": 726871, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "COLLECTION"},
                    {"title": "The Fast and the Furious", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 75},
                    {"title": "John Wick", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 2435487},
                    {"title": "The MonsterVerse", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 25439284},
                    {"title": "Mission: Impossible", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 131},
                    {"title": "Jurassic Park/World", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 29684902},
                    {"title": "Transformers", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 176},
                    {"title": "The Matrix", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 81},
                    {"title": "Avatar", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 884},
                    {"title": "Pirates of the Caribbean", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 10},
                    {"title": "Jason Bourne", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 36572888},
                    {"title": "Top Gun", "sortBy": "released", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 5022174}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{NUVIO_CDN}/Sagas_logo.webp",
                "coverImageUrl": f"{NUVIO_CDN}/Sagas_cover.webp",
                "heroBackdropUrl": f"{NUVIO_CDN}/Sagas_backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            }
        ],
        "pinToTop": True,
        "viewMode": "TABBED_GRID",
        "showAllTab": True,
        "focusGlowEnabled": True
    }

    # =========================================================================
    # 04 ✦ REGIONAL & SPOTLIGHT (Languages + Seasonal)
    # =========================================================================
    spotlight_collection = {
        "id": "collection-spotlight-regional",
        "title": "Regional & Spotlight",
        "folders": [
            {
                "id": "folder-bollywood-hindi",
                "title": "Bollywood 🇮🇳",
                "sources": [
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": bollywood_movie},
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": bollywood_series},
                    {
                        "title": "Top Bollywood Hits",
                        "sortBy": "popularity.desc",
                        "filters": {"withOriginalLanguage": "hi"},
                        "provider": "tmdb",
                        "mediaType": "MOVIE",
                        "tmdbSourceType": "DISCOVER"
                    }
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{NUVIO_CDN}/Bollywood_logo.webp",
                "coverImageUrl": f"{NUVIO_CDN}/Bollywood_cover.webp",
                "heroBackdropUrl": f"{NUVIO_CDN}/Bollywood_backdrop.webp",
                "catalogSources": [
                    {"type": "movie", "addonId": addon_id, "catalogId": bollywood_movie},
                    {"type": "series", "addonId": addon_id, "catalogId": bollywood_series}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-south-indian",
                "title": "South Indian Cinema",
                "sources": [
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": south_indian_movie},
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": south_indian_series},
                    {
                        "title": "South Indian Blockbusters",
                        "sortBy": "popularity.desc",
                        "filters": {"withOriginalLanguage": "te|ta|ml|kn"},
                        "provider": "tmdb",
                        "mediaType": "MOVIE",
                        "tmdbSourceType": "DISCOVER"
                    }
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{NUVIO_CDN}/SouthIndian_logo.webp",
                "coverImageUrl": f"{NUVIO_CDN}/SouthIndian_cover.webp",
                "heroBackdropUrl": f"{NUVIO_CDN}/SouthIndian_backdrop.webp",
                "catalogSources": [
                    {"type": "movie", "addonId": addon_id, "catalogId": south_indian_movie},
                    {"type": "series", "addonId": addon_id, "catalogId": south_indian_series}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-anime-spotlight",
                "title": "Anime Spotlight 🇯🇵",
                "sources": [
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": anime_series},
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": anime_movie},
                    {
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
                    }
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{K_IMG}/Anime/Popular/logo.webp",
                "coverImageUrl": f"{K_IMG}/Anime/Popular/cover.webp",
                "heroBackdropUrl": f"{K_IMG}/Anime/Popular/backdrop.webp",
                "catalogSources": [
                    {"type": "series", "addonId": addon_id, "catalogId": anime_series},
                    {"type": "movie", "addonId": addon_id, "catalogId": anime_movie}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-kdrama-korean",
                "title": "K-Drama & Korean Waves 🇰🇷",
                "sources": [
                    {"type": "series", "addonId": addon_id, "provider": "addon", "catalogId": kdrama_series},
                    {"type": "movie", "addonId": addon_id, "provider": "addon", "catalogId": kdrama_movie},
                    {
                        "title": "Trending K-Dramas",
                        "sortBy": "popularity.desc",
                        "filters": {"withOriginalLanguage": "ko"},
                        "provider": "tmdb",
                        "mediaType": "TV",
                        "tmdbSourceType": "DISCOVER"
                    },
                    {
                        "title": "Top Korean Cinema",
                        "sortBy": "vote_average.desc",
                        "filters": {"withOriginalLanguage": "ko", "voteCountGte": 200},
                        "provider": "tmdb",
                        "mediaType": "MOVIE",
                        "tmdbSourceType": "DISCOVER"
                    }
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{NUVIO_CDN}/KDrama_logo.webp",
                "coverImageUrl": f"{NUVIO_CDN}/KDrama_cover.webp",
                "heroBackdropUrl": f"{NUVIO_CDN}/KDrama_backdrop.webp",
                "catalogSources": [
                    {"type": "series", "addonId": addon_id, "catalogId": kdrama_series},
                    {"type": "movie", "addonId": addon_id, "catalogId": kdrama_movie}
                ],
                "focusGifEnabled": False
            },
            {
                "id": "folder-seasonal-holidays",
                "title": "Seasonal & Holiday Specials 🎃 🎄",
                "sources": [
                    {"title": "Christmas Holiday Collection", "sortBy": "rank", "sortHow": "asc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 20735298},
                    {"title": "Halloween & Spooky Season", "sortBy": "released", "sortHow": "desc", "provider": "trakt", "mediaType": "MOVIE", "traktListId": 3877179},
                    {"title": "Valentine's Day (Romance & Date Night)", "sortBy": "popularity.desc", "filters": {"withGenres": "10749", "voteCountGte": 150}, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "DISCOVER"},
                    {"title": "Summer Blockbusters & Popcorn Hits", "sortBy": "popularity.desc", "filters": {"withGenres": "28|12", "voteCountGte": 300}, "provider": "tmdb", "mediaType": "MOVIE", "tmdbSourceType": "DISCOVER"}
                ],
                "hideTitle": False,
                "tileShape": "landscape",
                "focusGifUrl": "",
                "titleLogoUrl": f"{NUVIO_CDN}/Seasonal_logo.webp",
                "coverImageUrl": f"{NUVIO_CDN}/Seasonal_cover.webp",
                "heroBackdropUrl": f"{NUVIO_CDN}/Seasonal_backdrop.webp",
                "catalogSources": [],
                "focusGifEnabled": False
            }
        ],
        "pinToTop": True,
        "viewMode": "TABBED_GRID",
        "showAllTab": True,
        "focusGlowEnabled": True
    }

    # Sanitize all collections to guarantee Nuvio never encounters a null catalogId:
    def _sanitize_collection(coll: dict, auto_hide_empty: bool = False) -> dict:
        cleaned_folders = []
        for f in coll.get("folders") or []:
            sources = [
                s for s in (f.get("sources") or [])
                if s.get("provider") != "addon" or s.get("catalogId")
            ]
            catalog_sources = [
                cs for cs in (f.get("catalogSources") or [])
                if cs.get("catalogId")
            ]
            if auto_hide_empty and not sources:
                continue
            folder_copy = dict(f)
            folder_copy["sources"] = sources
            folder_copy["catalogSources"] = catalog_sources
            cleaned_folders.append(folder_copy)
        coll_copy = dict(coll)
        coll_copy["folders"] = cleaned_folders
        return coll_copy

    discover_collection = _sanitize_collection(discover_collection)
    ott_collection = _sanitize_collection(ott_collection, auto_hide_empty=True)
    franchises_collection = _sanitize_collection(franchises_collection)
    spotlight_collection = _sanitize_collection(spotlight_collection)

    return [discover_collection, ott_collection, franchises_collection, spotlight_collection]


#----- Dynamic Nuvio Collection JSON for this token
@router.get("/{token}/nuvio-collection.json")
async def get_nuvio_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "full", token_data)


@router.get("/{token}/nuvio-discover-collection.json")
async def get_nuvio_discover_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "discover", token_data)


@router.get("/{token}/nuvio-ott-collection.json")
async def get_nuvio_ott_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "ott", token_data)


@router.get("/{token}/nuvio-franchises-collection.json")
async def get_nuvio_franchises_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "franchises", token_data)


@router.get("/{token}/nuvio-spotlight-collection.json")
async def get_nuvio_spotlight_collection(token: str, token_data: dict = Depends(verify_token)):
    return await build_nuvio_collection_data(token, "spotlight", token_data)


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
    imdb_id = parsed["imdb_id"] if not parsed["is_kitsu"] and not parsed["is_tmdb"] else None
    kitsu_id = parsed["kitsu_id"]
    tmdb_id = parsed["tmdb_id"]

    media = await db.get_media_details(
        imdb_id=imdb_id,
        kitsu_id=kitsu_id,
        tmdb_id=tmdb_id,
        media_type=media_type,
        projection={"telegram": 0, "seasons.episodes.telegram": 0}
    )
    if not media:
        fallback_meta = await _external_meta_for_id(id, parsed, media_type)
        return {"meta": fallback_meta or {}}

    if not _token_can_view(media.get("visibility") or "public", media.get("allowed_tokens") or [], token_data):
        return {"meta": {}}

    meta_id = id
    if parsed["is_kitsu"] and kitsu_id is not None:
        meta_id = f"kitsu:{kitsu_id}"
    elif parsed.get("is_tmdb") and tmdb_id is not None:
        meta_id = f"tmdb:{'movie' if (parsed.get('tmdb_media_type') or ('movie' if media_type == 'movie' else 'tv')) == 'movie' else 'tv'}:{tmdb_id}"
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
        "imdb_id": media.get("imdb_id") or (id if not parsed["is_kitsu"] and not parsed.get("is_tmdb") else ""),
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
                    "imdb_id": episode.get("imdb_id") or media.get("imdb_id") or (episode_id if parsed.get("is_tmdb") else (id if not parsed["is_kitsu"] else "")),
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
    for r in global_results:
        is_split = bool(r.get("is_split"))
        _, stream_title = format_stream_details(r["title"], r["quality"], r["size"], is_split=is_split)
        stream_name = sort_name = f"🌐 GLOBAL {r['quality']}"
        stream_title = f"{stream_title}\n📡 {r['source_chat']}"
        if is_split:
            kind = "zip parts" if r.get("is_zip") else "parts"
            stream_title += f" · 📦 {r.get('part_count', 0)} {kind}"
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
                    "title": "Your plan has expired.\nRenew it from the bot to continue watching.\n\n📖 Need help? Check the README / Setup Guide or contact your admin.",
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
                        "title": "First join the channel to stream it.\nThen wait 2 min for verification.\n\n📖 Read the README or contact your bot admin for setup instructions.",
                        "url": get_streambot_url()
                    }
                ]
            }

    if token_data.get("limit_exceeded"):
        limit_type = token_data["limit_exceeded"]

        title = (
            "🚫 Daily Limit Reached – Upgrade Required\n\n📖 Read the README or contact your bot admin to upgrade."
            if limit_type == "daily"
            else "🚫 Monthly Limit Reached – Upgrade Required\n\n📖 Read the README or contact your bot admin to upgrade."
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
        tmdb_id = parsed.get("tmdb_id")
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid Stremio ID format")

    if is_kitsu and kitsu_id is None:
        raise HTTPException(status_code=400, detail="Invalid Kitsu ID format")

    if not await _title_allowed(
        imdb_id=imdb_id,
        token_data=token_data,
        kitsu_id=kitsu_id,
        tmdb_id=tmdb_id,
        media_type=media_type,
    ):
        return {"streams": []}

    media_details = await db.get_media_details(
        imdb_id=imdb_id,
        season_number=season_num,
        episode_number=episode_num,
        kitsu_id=kitsu_id,
        tmdb_id=tmdb_id,
        media_type=media_type,
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
        return {"streams": [_donation()]}

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
    streams.append(_donation())
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
        "theme": get_theme(request.session.get("theme", DEFAULT_THEME), request.session.get("style", DEFAULT_STYLE)),
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
        for c in await db.get_custom_catalogs(slim=True):
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
