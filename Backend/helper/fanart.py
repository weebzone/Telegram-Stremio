import asyncio
import random
import time
from typing import Optional

import httpx

from Backend.helper.metadata import tmdb_api_key
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

_MOVIE_URL = "https://webservice.fanart.tv/v3/movies/{id}"
_TV_URL = "https://webservice.fanart.tv/v3/tv/{id}"
_EXTERNAL_IDS_URL = "https://api.themoviedb.org/3/tv/{id}/external_ids"

_MOVIE_FIELDS = {
    "poster": ["movieposter"],
    "logo": ["hdmovielogo", "movielogo", "clearlogo"],
    "background": ["moviebackground"],
}
_TV_FIELDS = {
    "poster": ["tvposter"],
    "logo": ["hdtvlogo", "clearlogo", "hdclearlogo"],
    "background": ["showbackground"],
}

def _preview(url: str) -> str:
    return url.replace("/fanart/", "/preview/", 1) if url else url


_CACHE_TTL = 6 * 3600
_cache: dict = {}
_tvdb_cache: dict = {}

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=12.0, follow_redirects=True)
    return _client


async def _fetch(url: str, params: dict) -> dict:
    now = time.monotonic()
    cached = _cache.get(url)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    try:
        client = await _get_client()
        resp = await client.get(url, params=params)
        data = resp.json() if resp.status_code == 200 else {}
    except Exception as e:
        LOGGER.warning(f"[FANART] fetch failed {url}: {e}")
        return {}
    _cache[url] = (now, data if isinstance(data, dict) else {})
    return _cache[url][1]


async def _resolve_tvdb(tmdb_id) -> Optional[int]:
    if not tmdb_id:
        return None
    if tmdb_id in _tvdb_cache:
        return _tvdb_cache[tmdb_id]
    key = tmdb_api_key()
    if not key:
        return None
    data = await _fetch(_EXTERNAL_IDS_URL.format(id=tmdb_id), {"api_key": key})
    tvdb = data.get("tvdb_id")
    _tvdb_cache[tmdb_id] = tvdb
    return tvdb


def _pick(items, shuffle: bool, interval: int, seed_key: str) -> str:
    items = [i for i in (items or []) if i.get("url")]
    if not items:
        return ""
    english = [i for i in items if (i.get("lang") or "").lower() == "en"]
    pool = english or items
    if not shuffle:
        return max(pool, key=lambda i: int(i.get("likes") or 0)).get("url", "")
    if interval <= 0:
        return random.choice(pool).get("url", "")
    bucket = int(time.time() // (interval * 60))
    return random.Random(f"{seed_key}:{bucket}").choice(pool).get("url", "")


async def fanart_artwork(imdb_id, tmdb_id, media_type) -> dict:
    settings = SettingsManager.current()
    api_key = settings.fanart_api_key
    if not api_key:
        return {}

    is_tv = media_type == "tv"
    if is_tv:
        lookup_id = await _resolve_tvdb(tmdb_id)
        url_tmpl, fields = _TV_URL, _TV_FIELDS
    else:
        lookup_id = imdb_id
        url_tmpl, fields = _MOVIE_URL, _MOVIE_FIELDS
    if not lookup_id:
        return {}

    data = await _fetch(url_tmpl.format(id=lookup_id), {"api_key": api_key})
    if not data:
        return {}

    shuffle = settings.fanart_shuffle
    interval = settings.fanart_shuffle_interval
    low_res_poster = settings.fanart_low_res_poster
    out = {}
    for target, keys in fields.items():
        items = []
        for k in keys:
            items = data.get(k) or []
            if items:
                break
        url = _pick(items, shuffle, interval, f"{lookup_id}:{target}")
        if url:
            out[target] = _preview(url) if low_res_poster and target == "poster" else url
    return out
