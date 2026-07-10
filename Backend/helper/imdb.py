import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = "https://v3-cinemeta.strem.io"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F" 
    "\U0001F300-\U0001F5FF"   
    "\U0001F680-\U0001F6FF"   
    "\U0001F700-\U0001FAFF"   
    "\U00002702-\U000027B0"  
    "\U000024C2-\U0001F251"   
    "\u2600-\u26FF"          
    "\u2700-\u27BF"          
    "\uFE00-\uFE0F"           
    "\U0001F1E0-\U0001F1FF"   
    "]+",
    re.UNICODE,
)

async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        return _client

def _cinemeta_type(media_type: str) -> str:
    return "series" if media_type in ("tvSeries", "tv", "series") else "movie"

async def _fetch_json(url: str) -> Optional[Dict[str, Any]]:
    try:
        client = await _get_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None

def _clean_search_query(query: str) -> str:
    q = _EMOJI_RE.sub(" ", query)
    q = re.sub(r"[^\w\s\-\'&:.]", " ", q)
    return re.sub(r"\s+", " ", q).strip()

def extract_first_year(year_string) -> int:
    if not year_string:
        return 0
    m = re.search(r"(\d{4})", str(year_string))
    return int(m.group(1)) if m else 0

async def search_title(query: str, type: str) -> Optional[Dict[str, Any]]:
    results = await search_title_multi(query=query, type=type, limit=1)
    return results[0] if results else None

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

async def get_detail(imdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    data = await _fetch_json(f"{BASE_URL}/meta/{_cinemeta_type(media_type)}/{imdb_id}.json")
    meta = (data or {}).get("meta")
    if not meta or not meta.get("name"):
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
        "type": meta.get("type") or media_type,
        "title": meta.get("name") or "",
        "plot": meta.get("description") or "",
        "genre": meta.get("genres") or meta.get("genre") or [],
        "releaseDetailed": {"year": year_value},
        "rating": {"star": float(meta.get("imdbRating") or 0)},
        "poster": meta.get("poster") or "",
        "background": meta.get("background") or "",
        "logo": meta.get("logo") or "",
        "runtime": meta.get("runtime") or 0,
        "director": meta.get("director") or [],
        "cast": meta.get("cast") or [],
        "videos": meta.get("videos") or [],
    }

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
