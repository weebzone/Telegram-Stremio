import asyncio
import re
from typing import Optional, List

import httpx

from Backend.logger import LOGGER

ANILIST_URL = "https://graphql.anilist.co"
ANIZIP_URL = "https://api.ani.zip/mappings"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_SEARCH_CACHE: dict = {}
_MAP_CACHE: dict = {}

_HTML_RE = re.compile(r"<[^>]+>")

_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english }
    seasonYear
    startDate { year }
    description(asHtml: false)
    genres
    averageScore
    duration
    coverImage { extraLarge large }
    bannerImage
  }
}
"""


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "Telegram-Stremio (+https://github.com/weebzone/Telegram-Stremio)"},
            )
        return _client


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", _HTML_RE.sub(" ", text)).strip()


def _season_queries(title: str, season: Optional[int]) -> List[str]:
    if season and int(season) > 1:
        return [f"{title} Season {season}", f"{title} {season}", title]
    return [title]


async def _anilist_request(search: str) -> Optional[dict]:
    try:
        client = await _get_client()
        resp = await client.post(ANILIST_URL, json={"query": _ANILIST_QUERY, "variables": {"search": search}})
        if resp.status_code != 200:
            return None
        return ((resp.json() or {}).get("data") or {}).get("Media")
    except Exception as e:
        LOGGER.warning(f"[ANIME] AniList search failed for '{search}': {e}")
        return None


async def search_anime(title: str, season: Optional[int] = None) -> Optional[dict]:
    cache_key = f"{title}::{season}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]
    media = None
    for query in _season_queries(title, season):
        media = await _anilist_request(query)
        if media:
            break
    _SEARCH_CACHE[cache_key] = media
    return media


async def get_anizip_mappings(anilist_id: int) -> Optional[dict]:
    if anilist_id in _MAP_CACHE:
        return _MAP_CACHE[anilist_id]
    try:
        client = await _get_client()
        resp = await client.get(ANIZIP_URL, params={"anilist_id": anilist_id})
        data = resp.json() if resp.status_code == 200 else None
    except Exception as e:
        LOGGER.warning(f"[ANIME] ani.zip mappings failed for {anilist_id}: {e}")
        data = None
    _MAP_CACHE[anilist_id] = data
    return data


def _anizip_image(images, cover_type: str) -> str:
    for img in images or []:
        if str(img.get("coverType", "")).lower() == cover_type.lower() and img.get("url"):
            return img["url"]
    return ""


# Resolve accurate anime metadata via AniList (search) + api.ani.zip (mappings/episodes).
# imdb_id may be returned as None when ani.zip lacks it — the caller resolves it from tmdb_id.
async def fetch_anime_metadata(title, season, episode, encoded_string, year=None, quality=None) -> Optional[dict]:
    media = await search_anime(title, season)
    if not media:
        LOGGER.info(f"[ANIME] No AniList match for '{title}' (season={season})")
        return None

    doc = await get_anizip_mappings(media["id"]) or {}
    mappings = doc.get("mappings") or {}

    imdb_id = mappings.get("imdb_id")
    tmdb_id = mappings.get("themoviedb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (ValueError, TypeError):
        tmdb_id = None
    if not imdb_id and not tmdb_id:
        LOGGER.info(f"[ANIME] No imdb/tmdb mapping for AniList {media['id']} ('{title}') -> fallback")
        return None

    titles = media.get("title") or {}
    name = titles.get("english") or titles.get("romaji") or title
    images = doc.get("images") or []
    cover = media.get("coverImage") or {}
    poster = cover.get("extraLarge") or cover.get("large") or _anizip_image(images, "Poster")
    backdrop = media.get("bannerImage") or _anizip_image(images, "Fanart") or _anizip_image(images, "Banner")
    logo = _anizip_image(images, "Clearlogo")

    ep = (doc.get("episodes") or {}).get(str(episode)) or {}
    ep_title = (ep.get("title") or {}).get("en") if isinstance(ep.get("title"), dict) else None

    year_value = media.get("seasonYear") or (media.get("startDate") or {}).get("year") or 0
    score = media.get("averageScore")
    duration = media.get("duration")

    return {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": name,
        "year": year_value,
        "rate": round(score / 10, 1) if score else 0,
        "description": _strip_html(media.get("description") or ""),
        "poster": poster or "",
        "backdrop": backdrop or "",
        "logo": logo or "",
        "genres": media.get("genres") or [],
        "media_type": "tv",
        "cast": [],
        "runtime": f"{duration} min" if duration else "",
        "season_number": season,
        "episode_number": episode,
        "episode_title": ep_title or f"S{season:02d}E{episode:02d}",
        "episode_backdrop": ep.get("image", "") or "",
        "episode_overview": ep.get("overview") or ep.get("summary") or "",
        "episode_released": ep.get("airDate") or ep.get("airdate") or "",
        "quality": quality,
        "encoded_string": encoded_string,
    }
