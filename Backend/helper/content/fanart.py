import asyncio
import random
import time
from typing import Optional
from urllib.parse import quote

import httpx

from Backend.helper.metadata import tmdb_api_key
from Backend.helper.settings.manager import SettingsManager
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


def _medium(url: str) -> str:
    return f"https://wsrv.nl/?url={quote(url, safe='')}&w=1280&output=webp&q=80" if url else url


_CACHE_TTL = 6 * 3600
_ERROR_TTL = 300
_CACHE_MAX = 4096
_cache: dict = {}
_tvdb_cache: dict = {}
_inflight: dict = {}

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_fetch_sem = asyncio.Semaphore(10)


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=4.0),
                follow_redirects=True,
            )
    return _client


async def _fetch_remote(url: str, params: dict) -> dict:
    try:
        async with _fetch_sem:
            client = await _get_client()
            resp = await client.get(url, params=params)
        data = resp.json() if resp.status_code == 200 else {}
        ttl = _CACHE_TTL if resp.status_code == 200 else _ERROR_TTL
    except Exception as e:
        LOGGER.warning(f"[FANART] fetch failed {url}: {e}")
        data, ttl = {}, _ERROR_TTL
    if not isinstance(data, dict):
        data, ttl = {}, _ERROR_TTL
    if len(_cache) >= _CACHE_MAX:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 10]:
            _cache.pop(k, None)
    _cache[url] = (time.monotonic(), data, ttl)
    return data


async def _fetch(url: str, params: dict) -> dict:
    cached = _cache.get(url)
    if cached and time.monotonic() - cached[0] < cached[2]:
        return cached[1]
    task = _inflight.get(url)
    if task is None:
        task = asyncio.create_task(_fetch_remote(url, params))
        _inflight[url] = task
        task.add_done_callback(lambda _t, _u=url: _inflight.pop(_u, None))
    return await task


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
    optimize = settings.fanart_low_res_poster
    out = {}
    for target, keys in fields.items():
        items = []
        for k in keys:
            items = data.get(k) or []
            if items:
                break
        url = _pick(items, shuffle, interval, f"{lookup_id}:{target}")
        if not url:
            continue
        if optimize:
            if target in ("poster", "logo"):
                url = _preview(url)
            elif target == "background":
                url = _medium(url)
        out[target] = url
    return out
