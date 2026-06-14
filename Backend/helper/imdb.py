import httpx
import re
import asyncio
from typing import Optional, Dict, Any, List

BASE_URL = "https://v3-cinemeta.strem.io"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Pre-compiled emoji / unicode-symbol strip pattern
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001FAFF"   # extended symbols
    "\U00002702-\U000027B0"   # dingbats
    "\U000024C2-\U0001F251"   # enclosed chars
    "\u2600-\u26FF"           # misc symbols
    "\u2700-\u27BF"           # dingbats block
    "\uFE00-\uFE0F"           # variation selectors
    "\U0001F1E0-\U0001F1FF"   # flags
    "]+",
    re.UNICODE,
)


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True
            )
        return _client


def _clean_search_query(query: str) -> str:
    """
    Strip emojis, decorative unicode symbols and non-essential punctuation
    from a search query so Cinemeta can find the title reliably.
    """
    q = _EMOJI_RE.sub(" ", query)
    # Keep word chars, spaces, hyphens, apostrophes, ampersands, colons
    q = re.sub(r"[^\w\s\-\'&:.]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def extract_first_year(year_string) -> int:
    if not year_string:
        return 0
    year_str = str(year_string)
    year_match = re.search(r'(\d{4})', year_str)
    if year_match:
        return int(year_match.group(1))
    return 0


async def search_title(query: str, type: str) -> Optional[Dict[str, Any]]:
    """
    Return the single top Cinemeta result (backward-compatible).
    Uses cleaned query to handle emojis / special chars in titles.
    """
    results = await search_title_multi(query=query, type=type, limit=1)
    return results[0] if results else None


async def search_title_multi(query: str, type: str, limit: int = 8) -> List[Dict[str, Any]]:
    """
    Return up to *limit* Cinemeta results for *query*.
    The caller is responsible for picking the best match via fuzzy scoring.
    """
    client = await _get_client()
    cinemeta_type = "series" if type == "tvSeries" else type

    # Clean the query before sending – emojis break the URL match
    cleaned = _clean_search_query(query)
    normalized = ".".join(cleaned.strip().lower().split())

    url = f"{BASE_URL}/catalog/{cinemeta_type}/imdb/search={normalized}.json"
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        data = resp.json()
        results: List[Dict[str, Any]] = []
        if data and "metas" in data and data["metas"]:
            for meta in data["metas"][:limit]:
                imdb_id = meta.get("imdb_id") or meta.get("id", "")
                if not imdb_id:
                    continue
                results.append({
                    "id": imdb_id,
                    "type": type,
                    "title": meta.get("name", ""),
                    "year": meta.get("releaseInfo", ""),
                    "poster": meta.get("poster", ""),
                })
        return results
    except Exception:
        return []


async def get_detail(imdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    client = await _get_client()
    cinemeta_type = "series" if media_type in ["tvSeries", "tv"] else "movie"

    try:
        url = f"{BASE_URL}/meta/{cinemeta_type}/{imdb_id}.json"
        resp = await client.get(url)

        if resp.status_code != 200:
            return None

        data = resp.json()
        meta = data.get("meta")
        if not meta:
            return None

        year_value = 0
        for field in ["year", "releaseInfo", "released"]:
            if meta.get(field):
                year_value = extract_first_year(meta[field])
                if year_value:
                    break

        return {
            "id": meta.get("imdb_id") or meta.get("id"),
            "moviedb_id": meta.get("moviedb_id"),
            "type": meta.get("type", media_type),
            "title": meta.get("name", ""),
            "plot": meta.get("description", ""),
            "genre": meta.get("genres") or meta.get("genre", []),
            "releaseDetailed": {"year": year_value},
            "rating": {"star": float(meta.get("imdbRating", 0) or 0)},
            "poster": meta.get("poster", ""),
            "background": meta.get("background", ""),
            "logo": meta.get("logo", ""),
            "runtime": meta.get("runtime") or 0,
            "director": meta.get("director", []),
            "cast": meta.get("cast", []),
            "videos": meta.get("videos", [])
        }

    except Exception:
        return None


async def get_season(imdb_id: str, season_id: int, episode_id: int) -> Optional[Dict[str, Any]]:
    client = await _get_client()
    try:
        url = f"{BASE_URL}/meta/series/{imdb_id}.json"
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if 'meta' in data and 'videos' in data['meta']:
            for video in data['meta']['videos']:
                if (str(video.get('season', '')) == str(season_id) and
                        str(video.get('episode', '')) == str(episode_id)):
                    return {
                        'title': video.get('title', f'Episode {episode_id}'),
                        'no': str(episode_id),
                        'season': str(season_id),
                        'image': video.get('thumbnail', ''),
                        'plot': video.get('overview', ''),
                        'released': video.get('released', '')
                    }
        return None
    except Exception:
        return None
