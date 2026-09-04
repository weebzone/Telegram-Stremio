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

from Backend import __version__, db
from Backend.config import Telegram
from Backend.helper.analytics import client_ip_from, record_client
from Backend.fastapi.security.tokens import verify_token
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_theme, __x7
from Backend.helper.fanart import fanart_artwork
from Backend.helper.global_search import global_search, is_global_search_enabled
from Backend.helper.metadata.providers.cinemeta import get_detail, get_season
from Backend.helper.metadata import resolve_cover_url, COMBINED_SEASON, COMBINED_EPISODE_BASE
from Backend.helper.split_files import parse_combined_episodes, combined_name_key
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.subtitles import get_subtitles_for, stremio_subtitle_entries
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, get_streambot_url

router = APIRouter(prefix="/stremio", tags=["Stremio Addon"])
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
def _token_can_view(mode: str, allowed_tokens: list, token_data: dict) -> bool:
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
def _visibility_query(token_data: dict) -> dict:
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
    doc = await db.get_media_details(imdb_id=imdb_id, kitsu_id=kitsu_id)
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


def _poster_url(imdb_id: str, fallback: str) -> str:
    settings = SettingsManager.current()
    if imdb_id:
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
        "poster": _poster_url(imdb, item.get("poster")),
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
                        "extra": [{"name": "skip"}],
                        "extraSupported": ["skip"],
                    })
                if has_series:
                    catalogs.append({
                        "type": "series",
                        "id": f"custom_{catalog_id}",
                        "name": catalog_name,
                        "extra": [{"name": "skip"}],
                        "extraSupported": ["skip"],
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
        if id.startswith("custom_"):
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
            start = (page - 1) * PAGE_SIZE
            items = await db.get_documents(visible_items[start:start + PAGE_SIZE])
            items = [it for it in items if _token_can_view(it.get("visibility") or "public", it.get("allowed_tokens") or [], token_data)]
        elif search_query:
            search_results = await db.search_documents(
                query=search_query, page=page, page_size=PAGE_SIZE,
                extra_filter=_merge_filters(_visibility_query(token_data), _not_exclusive_clause(allow_searchable=True)),
            )
            all_items = search_results.get("results", [])
            db_media_type = "tv" if media_type == "series" else "movie"
            items = [item for item in all_items if item.get("media_type") == db_media_type]
        else:
            if "latest" in id:
                sort_params = [("updated_on", "desc")]
            elif "top" in id:
                sort_params = [("rating", "desc")]
            else:
                sort_params = [("updated_on", "desc")]

            vis_filter = _merge_filters(_visibility_query(token_data), _not_exclusive_clause())
            if media_type == "movie":
                data = await db.sort_movies(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, extra_filter=vis_filter)
                items = data.get("movies", [])
            else:
                data = await db.sort_tv_shows(sort_params, page, PAGE_SIZE, genre_filter=genre_filter, extra_filter=vis_filter)
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

    media = await db.get_media_details(imdb_id=imdb_id, kitsu_id=kitsu_id)
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
        "poster": _poster_url(media.get("imdb_id") or imdb_id, media.get("poster")),
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


def _streams_from_global_results(token: str, global_results: list) -> list:
    streams = []
    for r in global_results:
        is_split = bool(r.get("is_split"))
        _, stream_title = format_stream_details(r["title"], r["quality"], r["size"], is_split=is_split)
        stream_name = f"🌐 GLOBAL {r['quality']}"
        stream_title = f"{stream_title}\n📡 {r['source_chat']}"
        if is_split:
            kind = "zip parts" if r.get("is_zip") else "parts"
            stream_title += f" · 📦 {r.get('part_count', 0)} {kind}"
        url = f"{SettingsManager.current().base_url}/dl/{token}/{r['token']}/{quote(r['title'])}"
        size_bytes = parse_size_to_bytes(r.get("size", ""))
        streams.append({"name": stream_name, "title": stream_title, "url": url, "size_bytes": size_bytes})
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

                if combined:
                    label = "Full" if combined.get("start") is None else f"E{combined['start']:02d}-E{combined['end']:02d}"
                    if label.lower() not in stream_name.lower():
                        stream_name = f"{stream_name} {label}"

                original_url = f"{SettingsManager.current().base_url}/dl/{token}/{quality.get('id')}/video.mkv"
                proxy_url = build_proxy_url(original_url)

                if SettingsManager.current().show_proxy_and_non_proxy_both and proxy_url:
                    streams.append({"name": f"{stream_name} (Proxy)", "title": stream_title, "url": proxy_url, "size_bytes": size_bytes, "episode_start": episode_start, "name_key": name_key})
                    streams.append({"name": f"{stream_name} (Direct)", "title": stream_title, "url": original_url, "size_bytes": size_bytes, "episode_start": episode_start, "name_key": name_key})
                elif proxy_url:
                    streams.append({"name": stream_name, "title": stream_title, "url": proxy_url, "size_bytes": size_bytes, "episode_start": episode_start, "name_key": name_key})
                else:
                    streams.append({"name": stream_name, "title": stream_title, "url": original_url, "size_bytes": size_bytes, "episode_start": episode_start, "name_key": name_key})
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
        filtered = [s for s in streams if stream_res_label(s.get("name", "")) in quality_filter]
        if filtered:
            streams = filtered

    if not streams:
        return {"streams": [__x7()]}

    ascending = config.get("quality_sort") == "asc"
    if is_combined:
        streams.sort(key=lambda s: s.get("episode_start", 0))
        streams.sort(key=lambda s: s.get("name_key", ""))
        streams.sort(key=lambda s: get_resolution_priority(s.get("name", "")), reverse=not ascending)
    else:
        streams.sort(
            key=lambda s: (get_resolution_priority(s.get("name", "")), s.get("size_bytes", 0)),
            reverse=not ascending
        )
    name_count: dict = {}
    for s in streams:
        name_count[s["name"]] = name_count.get(s["name"], 0) + 1

    seen: dict = {}
    for s in streams:
        if name_count[s["name"]] > 1:
            seen[s["name"]] = seen.get(s["name"], 0) + 1
            s["name"] = f"{s['name']} ({seen[s['name']]})"
    streams.insert(0, __x7())
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
