import asyncio
import re
from typing import Optional, Dict, Any, List

import httpx

BASE_URL = "https://v3-cinemeta.strem.io"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Strips emojis and decorative unicode symbols that break Cinemeta queries.
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


# Return a shared, lazily-created httpx client.
async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        return _client


# Map a generic media type to Cinemeta's catalog type.
def _cinemeta_type(media_type: str) -> str:
    return "series" if media_type in ("tvSeries", "tv", "series") else "movie"


# GET a Cinemeta URL and return parsed JSON, or None on any failure.
async def _fetch_json(url: str) -> Optional[Dict[str, Any]]:
    try:
        client = await _get_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


# Strip emojis and decorative punctuation so Cinemeta can match the title.
def _clean_search_query(query: str) -> str:
    q = _EMOJI_RE.sub(" ", query)
    q = re.sub(r"[^\w\s\-\'&:.]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


# Return the first 4-digit year found in a value, else 0.
def extract_first_year(year_string) -> int:
    if not year_string:
        return 0
    m = re.search(r"(\d{4})", str(year_string))
    return int(m.group(1)) if m else 0


# Return the single top Cinemeta result (backward-compatible helper).
async def search_title(query: str, type: str) -> Optional[Dict[str, Any]]:
    results = await search_title_multi(query=query, type=type, limit=1)
    return results[0] if results else None


# Return up to *limit* Cinemeta results; caller picks the best via fuzzy scoring.
async def search_title_multi(query: str, type: str, limit: int = 8) -> List[Dict[str, Any]]:
    cleaned = _clean_search_query(query)
    normalized = ".".join(cleaned.strip().lower().split())
    url = f"{BASE_URL}/catalog/{_cinemeta_type(type)}/imdb/search={normalized}.json"

    data = await _fetch_json(url)
    metas = (data or {}).get("metas") or []
    results: List[Dict[str, Any]] = []
    for meta in metas[:limit]:
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


# Fetch full Cinemeta metadata for an IMDb id.
async def get_detail(imdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    data = await _fetch_json(f"{BASE_URL}/meta/{_cinemeta_type(media_type)}/{imdb_id}.json")
    meta = (data or {}).get("meta")
    if not meta:
        return None

    year_value = 0
    for field in ("year", "releaseInfo", "released"):
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
        "videos": meta.get("videos", []),
    }


# Fetch a specific season/episode entry from a Cinemeta series.
async def get_season(imdb_id: str, season_id: int, episode_id: int) -> Optional[Dict[str, Any]]:
    data = await _fetch_json(f"{BASE_URL}/meta/series/{imdb_id}.json")
    videos = (data or {}).get("meta", {}).get("videos") or []
    for video in videos:
        if str(video.get("season", "")) == str(season_id) and str(video.get("episode", "")) == str(episode_id):
            return {
                "title": video.get("title", f"Episode {episode_id}"),
                "no": str(episode_id),
                "season": str(season_id),
                "image": video.get("thumbnail", ""),
                "plot": video.get("overview", ""),
                "released": video.get("released", ""),
            }
    return None
