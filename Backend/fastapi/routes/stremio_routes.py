import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, unquote

import PTN
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant

from Backend import __version__, db
from Backend.config import Telegram
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.global_search import global_search, is_global_search_enabled
from Backend.helper.imdb import get_detail, get_season
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, get_streambot_url

router = APIRouter(prefix="/stremio", tags=["Stremio Addon"])

#----- Addon configuration
ADDON_NAME = "Telegram"
ADDON_VERSION = __version__
PAGE_SIZE = 15

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


#----- Available catalog genres
GENRES = [
    "Action", "Adventure", "Animation", "Biography", "Comedy",
    "Crime", "Documentary", "Drama", "Family", "Fantasy",
    "History", "Horror", "Music", "Mystery", "Romance",
    "Sci-Fi", "Sport", "Thriller", "War", "Western"
]


#----- Map an internal media item into a Stremio meta object
def convert_to_stremio_meta(item: dict) -> dict:
    media_type = "series" if item.get("media_type") == "tv" else "movie"

    meta = {
        "id": item.get('imdb_id'),
        "type": media_type,
        "name": item.get("title"),
        "poster": item.get("poster") or "",
        "logo": item.get("logo") or "",
        "year": item.get("release_year"),
        "releaseInfo": str(item.get("release_year", "")),
        "imdb_id": item.get("imdb_id", ""),
        "moviedb_id": item.get("tmdb_id", ""),
        "background": item.get("backdrop") or "",
        "genres": item.get("genres") or [],
        "imdbRating": str(item.get("rating") or ""),
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
            return datetime(int(year), 1, 1).isoformat() + "Z"
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


#----- Manifest describing the addon's catalogs/resources for this token
@router.get("/{token}/manifest.json")
async def get_manifest(token: str, token_data: dict = Depends(verify_token)):
    if SettingsManager.current().hide_catalog:
        resources = ["stream"]
        catalogs = []
    else:
        resources = ["catalog", "meta", "stream"]
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
            custom_catalogs = await db.get_custom_catalogs(visible_only=True)
            for catalog in custom_catalogs:
                items = catalog.get("items") or []
                has_movie = any(i.get("media_type") == "movie" for i in items)
                has_series = any(i.get("media_type") == "tv" for i in items)
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


    addon_name = ADDON_NAME
    addon_desc = "Streams movies and series from your Telegram."
    addon_version = ADDON_VERSION
    expiry_obj = None

    if SettingsManager.current().subscription:
        user_id = token_data.get("user_id")
        if user_id:
            try:
                user = await db.get_user(int(user_id))
                if user and user.get("subscription_status") == "active":
                    expiry_obj = user.get("subscription_expiry")
                    if expiry_obj:
                        expiry_str = expiry_obj.strftime("%d %b %Y").lstrip("0")
                        addon_name = f"{ADDON_NAME} — Expires {expiry_str}"
                        addon_desc = (
                            f"📅 Subscription active until {expiry_str}.\n"
                            f"Streams movies and series from your Telegram."
                        )
                        epoch_tag = format(int(expiry_obj.timestamp()) & 0xFFFF, "x")
                        addon_version = f"{ADDON_VERSION}-{epoch_tag}"
                    else:
                        addon_name = f"{ADDON_NAME} — Active"
                        addon_desc = "✅ Subscription active.\nStreams movies and series from your Telegram."
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
        "idPrefixes": ["tt"],
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
            if not catalog or not catalog.get("visible", True):
                return {"metas": []}

            db_media_type = "tv" if media_type == "series" else "movie"
            data = await db.get_custom_catalog_items(
                catalog_id=catalog_id,
                media_type=db_media_type,
                page=page,
                page_size=PAGE_SIZE,
            )
            items = data.get("items", [])
        elif search_query:
            search_results = await db.search_documents(query=search_query, page=page, page_size=PAGE_SIZE)
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

            if media_type == "movie":
                data = await db.sort_movies(sort_params, page, PAGE_SIZE, genre_filter=genre_filter)
                items = data.get("movies", [])
            else:
                data = await db.sort_tv_shows(sort_params, page, PAGE_SIZE, genre_filter=genre_filter)
                items = data.get("tv_shows", [])
    except Exception:
        return {"metas": []}

    metas = [convert_to_stremio_meta(item) for item in items]
    return {"metas": metas}


#----- Detailed metadata for a title, including series episode list
@router.get("/{token}/meta/{media_type}/{id}.json")
async def get_meta(token: str, media_type: str, id: str, token_data: dict = Depends(verify_token)):
    if SettingsManager.current().hide_catalog:
        raise HTTPException(status_code=404, detail="Catalog disabled")

    imdb_id = id

    media = await db.get_media_details(imdb_id=imdb_id)
    if not media:
        return {"meta": {}}

    meta_obj = {
        "id": id,
        "type": "series" if media.get("media_type") == "tv" else "movie",
        "name": media.get("title", ""),
        "description": media.get("description", ""),
        "year": str(media.get("release_year", "")),
        "imdbRating": str(media.get("rating", "")),
        "genres": media.get("genres", []),
        "poster": media.get("poster", ""),
        "logo": media.get("logo", ""),
        "background": media.get("backdrop", ""),
        "imdb_id": media.get("imdb_id", ""),
        "releaseInfo": str(media.get("release_year", "")),
        "moviedb_id": media.get("tmdb_id", ""),
        "cast": media.get("cast") or [],
        "runtime": media.get("runtime") or "",
    }

    if media.get("media_type") == "movie":
        released_date = format_released_date(media)
        if released_date:
            meta_obj["released"] = released_date

    #----- Series episodes
    if media_type == "series" and "seasons" in media:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        videos = []
        for season in sorted(media.get("seasons", []), key=lambda s: s.get("season_number")):
            for episode in sorted(season.get("episodes", []), key=lambda e: e.get("episode_number")):
                episode_id = f"{id}:{season['season_number']}:{episode['episode_number']}"
                videos.append({
                    "id": episode_id,
                    "title": episode.get("title", f"Episode {episode['episode_number']}"),
                    "season": season.get("season_number"),
                    "episode": episode.get("episode_number"),
                    "overview": episode.get("overview") or "No description available for this episode yet.",
                    "released": episode.get("released") or yesterday,
                    "thumbnail": episode.get("episode_backdrop") or "https://raw.githubusercontent.com/weebzone/Colab-Tools/refs/heads/main/no_episode_backdrop.png",
                    "imdb_id": episode.get("imdb_id") or media.get("imdb_id"),
                })
        meta_obj["videos"] = videos
    return {"meta": meta_obj}

#----- Collect Global Search streams for a title/episode via IMDb lookup
async def _global_streams_for(token: str, imdb_id: str, media_type: str, season_num: Optional[int], episode_num: Optional[int]) -> list:
    imdb_media_type = "tvSeries" if media_type == "series" else "movie"

    detail = await get_detail(imdb_id=imdb_id, media_type=imdb_media_type)
    if not detail or not detail.get("title"):
        return []

    expected_title = detail["title"]
    year = (detail.get("releaseDetailed") or {}).get("year") or None

    if season_num is not None and episode_num is not None:
        try:
            await get_season(imdb_id=imdb_id, season_id=season_num, episode_id=episode_num)
        except Exception:
            pass

    try:
        global_results = await global_search(
            expected_title,
            SettingsManager.current().auth_channels,
            year=year,
            season=season_num,
            episode=episode_num,
        )
    except Exception as e:
        LOGGER.error(f"[GLOBAL SEARCH] search failed for '{expected_title}': {e}")
        return []

    streams = []
    for r in global_results:
        _, stream_title = format_stream_details(r["title"], r["quality"], r["size"], is_split=False)
        stream_name = f"🌐 GLOBAL {r['quality']}"
        stream_title = f"{stream_title}\n📡 {r['source_chat']}"
        url = f"{SettingsManager.current().base_url}/dl/{token}/{r['token']}/{quote(r['title'])}"
        streams.append({"name": stream_name, "title": stream_title, "url": url})
    return streams


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
    token_data: dict = Depends(verify_token)
):

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

    #----- Subscription users must currently be members of the configured group
    if SettingsManager.current().subscription:
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
        parts = id.split(":")
        imdb_id = parts[0]
        season_num = int(parts[1]) if len(parts) > 1 else None
        episode_num = int(parts[2]) if len(parts) > 2 else None
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid Stremio ID format")

    media_details = await db.get_media_details(
        imdb_id=imdb_id,
        season_number=season_num,
        episode_number=episode_num
    )

    streams = []

    if media_details and "telegram" in media_details:
        for quality in media_details.get("telegram", []):
            if quality.get("id"):
                filename = quality.get("name", "")
                quality_str = quality.get("quality", "HD")
                size = quality.get("size", "")

                stream_name, stream_title = format_stream_details(
                    filename, quality_str, size, is_split=bool(quality.get("group_key"))
                )

                original_url = f"{SettingsManager.current().base_url}/dl/{token}/{quality.get('id')}/video.mkv"
                proxy_url = f"{SettingsManager.current().http_proxy_url}{original_url}" if SettingsManager.current().http_proxy_url else None

                if SettingsManager.current().show_proxy_and_non_proxy_both and proxy_url:
                    streams.append({
                        "name": f"{stream_name} (Proxy)",
                        "title": stream_title,
                        "url": proxy_url
                    })
                    streams.append({
                        "name": f"{stream_name} (Direct)",
                        "title": stream_title,
                        "url": original_url
                    })
                elif proxy_url:
                    streams.append({
                        "name": stream_name,
                        "title": stream_title,
                        "url": proxy_url
                    })
                else:
                    streams.append({
                        "name": stream_name,
                        "title": stream_title,
                        "url": original_url
                    })
    elif is_global_search_enabled():
        try:
            streams.extend(
                await _global_streams_for(token, imdb_id, media_type, season_num, episode_num)
            )
        except Exception as e:
            LOGGER.error(f"[GLOBAL SEARCH] stream search failed for {imdb_id}: {e}")

    if not streams:
        return {"streams": []}

    streams.sort(
        key=lambda s: get_resolution_priority(s.get("name", "")),
        reverse=True
    )
    name_count: dict = {}
    for s in streams:
        name_count[s["name"]] = name_count.get(s["name"], 0) + 1

    seen: dict = {}
    for s in streams:
        if name_count[s["name"]] > 1:
            seen[s["name"]] = seen.get(s["name"], 0) + 1
            s["name"] = f"{s['name']} ({seen[s['name']]})"
    return {"streams": streams}

#----- Configure/install landing page rendered as HTML for a token
@router.get("/{token}/configure")
async def configure_addon(token: str):
    manifest_url = f"{SettingsManager.current().base_url}/stremio/{token}/manifest.json"
    web_install_url = f"https://web.stremio.com/#/?addon_manifest={quote(manifest_url, safe='')}"

    #----- Fetch user info for display
    token_doc = await db.get_api_token(token)
    user_name = "Unknown"
    expiry_str = "N/A"
    status_color = "#ef4444"
    status_text = "Unknown"

    if token_doc:
        uid = token_doc.get("user_id")
        if uid:
            try:
                user = await db.get_user(int(uid))
                if user:
                    user_name = user.get("first_name") or user.get("username") or f"User {uid}"
                    sub_status = user.get("subscription_status", "")
                    expiry = user.get("subscription_expiry")
                    if expiry:
                        expiry_str = expiry.strftime("%d %b %Y").lstrip("0")
                    if sub_status == "active":
                        status_color = "#22c55e"
                        status_text = "✅ Active"
                    else:
                        status_color = "#ef4444"
                        status_text = "🔴 Expired"
            except Exception:
                pass

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Update Telegram Stremio Addon</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f0f1a; color: #e2e8f0;
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
      padding: 24px;
    }}
    .card {{
      background: #1e1e2e; border: 1px solid #2d2d44; border-radius: 16px;
      padding: 40px 32px; max-width: 480px; width: 100%; text-align: center;
    }}
    .logo {{ font-size: 48px; margin-bottom: 12px; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; color: #f8fafc; margin-bottom: 6px; }}
    .sub-title {{ color: #94a3b8; font-size: 0.9rem; margin-bottom: 28px; }}
    .info-row {{
      display: flex; justify-content: space-between; align-items: center;
      background: #2a2a3e; border-radius: 10px; padding: 12px 16px;
      margin-bottom: 12px; font-size: 0.9rem;
    }}
    .info-label {{ color: #94a3b8; }}
    .info-val {{ font-weight: 600; color: #f1f5f9; }}
    .status-badge {{
      display: inline-block; padding: 2px 10px; border-radius: 999px;
      font-size: 0.8rem; font-weight: 700;
      background: {status_color}22; color: {status_color};
    }}
    .btn-update {{
      display: block; width: 100%;
      background: linear-gradient(135deg, #7c3aed, #4f46e5);
      color: white; font-weight: 700; font-size: 1rem;
      padding: 14px 24px; border-radius: 12px; border: none;
      cursor: pointer; text-decoration: none; margin: 28px 0 12px;
      transition: opacity 0.2s;
    }}
    .btn-update:hover {{ opacity: 0.85; }}
    .btn-web {{
      display: block; color: #6366f1; font-size: 0.85rem;
      text-decoration: underline; margin-bottom: 20px;
    }}
    .steps {{
      background: #2a2a3e; border-radius: 10px; padding: 14px 18px;
      margin: 16px 0; text-align: left; font-size: 0.85rem; color: #cbd5e1;
    }}
    .steps b {{ color: #f1f5f9; }}
    .steps ol {{ margin-top: 8px; margin-left: 18px; line-height: 1.8; }}
    .url-box {{
      background: #111827; border: 1px solid #374151; border-radius: 8px;
      padding: 10px 14px; font-family: monospace; font-size: 0.75rem;
      color: #94a3b8; word-break: break-all; text-align: left; margin-top: 16px;
    }}
    .btn-copy {{
      margin-top: 10px; width: 100%; padding: 10px;
      background: #1e293b; border: 1px solid #374151; color: #94a3b8;
      border-radius: 8px; cursor: pointer; font-size: 0.85rem; transition: all 0.2s;
    }}
    .btn-copy:hover {{ background: #334155; color: #f1f5f9; }}
    .hint {{ color: #64748b; font-size: 0.78rem; margin-top: 6px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">🎬</div>
    <h1>Telegram Stremio Addon</h1>
    <p class="sub-title">Click the button below to install or update your addon in Stremio.</p>

    <div class="info-row">
      <span class="info-label">User</span>
      <span class="info-val">{user_name}</span>
    </div>
    <div class="info-row">
      <span class="info-label">Status</span>
      <span class="status-badge">{status_text}</span>
    </div>
    <div class="info-row">
      <span class="info-label">Expires</span>
      <span class="info-val">{expiry_str}</span>
    </div>

    <a href="{web_install_url}" class="btn-update" target="_blank">
      ⚡ Install / Update in Stremio
    </a>

    <div class="steps">
      <b>Or install manually:</b>
      <ol>
        <li>Open Stremio → <b>Add-ons</b> tab</li>
        <li>Click the <b>🔍 Search / URL</b> icon</li>
        <li>Paste the URL below and press Enter</li>
      </ol>
    </div>

    <div class="url-box" id="murl">{manifest_url}</div>
    <button onclick="copyUrl()" class="btn-copy">📋 Copy URL</button>
    <script>
      function copyUrl() {{
        navigator.clipboard.writeText('{manifest_url}').then(() => {{
          const b = document.querySelector('.btn-copy');
          b.textContent = '✅ Copied!';
          setTimeout(() => b.textContent = '📋 Copy URL', 2000);
        }});
      }}
    </script>
  </div>
</body>
</html>"""
    return HTMLResponse(html)
