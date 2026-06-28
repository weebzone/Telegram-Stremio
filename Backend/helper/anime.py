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

_ANILIST_FIELDS = """
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
"""

_ANILIST_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
""" + _ANILIST_FIELDS + """
  }
}
"""

_ANILIST_MOVIE_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME, format: MOVIE) {
""" + _ANILIST_FIELDS + """
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


async def _anilist_request(search: str, query: str = _ANILIST_QUERY) -> Optional[dict]:
    try:
        client = await _get_client()
        resp = await client.post(ANILIST_URL, json={"query": query, "variables": {"search": search}})
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


async def search_anime_movie(title: str) -> Optional[dict]:
    cache_key = f"movie::{title}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]
    media = await _anilist_request(title, _ANILIST_MOVIE_QUERY)
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


# Build the fields shared by anime movie/TV payloads from AniList + ani.zip data.
# imdb_id may be None when ani.zip lacks it — the caller resolves it from tmdb_id.
def _common_payload(media: dict, doc: dict, title: str) -> dict:
    mappings = doc.get("mappings") or {}
    tmdb_id = mappings.get("themoviedb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (ValueError, TypeError):
        tmdb_id = None

    titles = media.get("title") or {}
    images = doc.get("images") or []
    cover = media.get("coverImage") or {}
    score = media.get("averageScore")
    duration = media.get("duration")
    return {
        "tmdb_id": tmdb_id,
        "imdb_id": mappings.get("imdb_id"),
        "title": titles.get("english") or titles.get("romaji") or title,
        "year": media.get("seasonYear") or (media.get("startDate") or {}).get("year") or 0,
        "rate": round(score / 10, 1) if score else 0,
        "description": _strip_html(media.get("description") or ""),
        "poster": cover.get("extraLarge") or cover.get("large") or _anizip_image(images, "Poster"),
        "backdrop": media.get("bannerImage") or _anizip_image(images, "Fanart") or _anizip_image(images, "Banner"),
        "logo": _anizip_image(images, "Clearlogo"),
        "genres": media.get("genres") or [],
        "cast": [],
        "runtime": f"{duration} min" if duration else "",
    }


# Resolve accurate anime TV metadata via AniList (search) + api.ani.zip (mappings/episodes).
async def fetch_anime_metadata(title, season, episode, encoded_string, year=None, quality=None) -> Optional[dict]:
    media = await search_anime(title, season)
    if not media:
        LOGGER.info(f"[ANIME] No AniList match for '{title}' (season={season})")
        return None

    doc = await get_anizip_mappings(media["id"]) or {}
    payload = _common_payload(media, doc, title)
    if not payload["imdb_id"] and not payload["tmdb_id"]:
        LOGGER.info(f"[ANIME] No imdb/tmdb mapping for AniList {media['id']} ('{title}') -> fallback")
        return None

    ep = (doc.get("episodes") or {}).get(str(episode)) or {}
    ep_title = (ep.get("title") or {}).get("en") if isinstance(ep.get("title"), dict) else None

    payload.update({
        "media_type": "tv",
        "season_number": season,
        "episode_number": episode,
        "episode_title": ep_title or f"S{season:02d}E{episode:02d}",
        "episode_backdrop": ep.get("image", "") or "",
        "episode_overview": ep.get("overview") or ep.get("summary") or "",
        "episode_released": ep.get("airDate") or ep.get("airdate") or "",
        "quality": quality,
        "encoded_string": encoded_string,
    })
    return payload


# Resolve accurate anime movie metadata via AniList (format MOVIE) + api.ani.zip.
async def fetch_anime_movie_metadata(title, encoded_string, year=None, quality=None) -> Optional[dict]:
    media = await search_anime_movie(title)
    if not media:
        LOGGER.info(f"[ANIME] No AniList movie match for '{title}'")
        return None

    doc = await get_anizip_mappings(media["id"]) or {}
    payload = _common_payload(media, doc, title)
    if not payload["imdb_id"] and not payload["tmdb_id"]:
        LOGGER.info(f"[ANIME] No imdb/tmdb mapping for AniList movie {media['id']} ('{title}') -> fallback")
        return None

    payload.update({"media_type": "movie", "quality": quality, "encoded_string": encoded_string})
    return payload
