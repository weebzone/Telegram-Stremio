"""Cinemeta / IMDb metadata provider (via v3-cinemeta.strem.io)."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx

from Backend.helper.metadata.common import (
    ensure_media_ids,
    CINEMETA_THRESHOLD,
    IMDB_CACHE,
    STRONG_MATCH,
    cached_call,
    format_imdb_images,
    logo_from_imdb,
    score_candidate_aliases,
    year_from_str,
    build_query_variants,
)
from Backend.logger import LOGGER

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
    return year_from_str(year_string)


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
    ctype = _cinemeta_type(media_type)
    url = f"{BASE_URL}/meta/{ctype}/{imdb_id}.json"
    data = await _fetch_json(url)
    meta = (data or {}).get("meta")
    if not meta:
        return None
    return {
        "id": imdb_id,
        "imdb_id": imdb_id,
        "title": meta.get("name", ""),
        "plot": meta.get("description", ""),
        "poster": meta.get("poster", ""),
        "background": meta.get("background", ""),
        "logo": meta.get("logo", ""),
        "genre": meta.get("genres") or meta.get("genre") or [],
        "cast": meta.get("cast") or [],
        "runtime": meta.get("runtime"),
        "rating": {"star": float(meta.get("imdbRating") or 0) or 0},
        "releaseDetailed": {"year": extract_first_year(meta.get("releaseInfo"))},
        "moviedb_id": meta.get("moviedb_id") or meta.get("tmdb_id"),
        "type": media_type,
        "videos": meta.get("videos") or [],
    }


async def get_season(imdb_id: str, season_id, episode_id) -> Dict[str, Any]:
    ctype = "series"
    url = f"{BASE_URL}/meta/{ctype}/{imdb_id}.json"
    data = await _fetch_json(url)
    meta = (data or {}).get("meta") or {}
    videos = meta.get("videos") or []
    for v in videos:
        try:
            if int(v.get("season") or -1) == int(season_id) and int(v.get("episode") or -1) == int(episode_id):
                return {
                    "title": v.get("title") or v.get("name") or f"S{int(season_id):02d}E{int(episode_id):02d}",
                    "image": v.get("thumbnail") or "",
                    "plot": v.get("overview") or v.get("description") or "",
                    "released": v.get("released") or v.get("firstAired") or "",
                }
        except (TypeError, ValueError):
            continue
    return {}


async def safe_search(title: str, type_: str, year: Optional[int] = None) -> str | None:
    """Return best-matching IMDb id or None."""
    is_tv = type_ != "movie"
    search_year = None if is_tv else year
    cache_key = f"imdb::{type_}::{title}::{year}"

    async def _produce():
        query_variants = build_query_variants(title, search_year)
        best_id: str | None = None
        best_score = 0.0
        best_title = ""
        year_reliable = not is_tv

        for query in query_variants:
            try:
                results = await search_title_multi(query=query, type=type_, limit=8)
                for r in results:
                    # Cinemeta search rows are thin; still score primary + any alias-like fields
                    aliases = []
                    for key in ("aka", "aliases", "alternateNames", "genres"):
                        pass  # genres are not aliases
                    for key in ("aka", "aliases", "alternateNames", "name"):
                        val = r.get(key)
                        if not val or key == "name":
                            continue
                        if isinstance(val, list):
                            aliases.extend(val)
                        else:
                            aliases.append(val)
                    score = score_candidate_aliases(
                        title, year, r.get("title", "") or r.get("name", ""),
                        year_from_str(r.get("year", "")),
                        aliases=aliases,
                        year_reliable=year_reliable, year_lower_bound=is_tv,
                    )
                    if score > best_score:
                        best_score, best_id, best_title = score, r.get("id"), r.get("title", "")
                    if not is_tv and best_score >= STRONG_MATCH:
                        break
            except Exception as e:
                LOGGER.warning(f"Cinemeta search variant '{query}' [{type_}] failed: {e}")
            if not is_tv and best_score >= STRONG_MATCH:
                break

        if best_score >= CINEMETA_THRESHOLD and best_id:
            LOGGER.info(
                f"Cinemeta match: '{title}' (year={year}) -> '{best_title}' [{best_id}] "
                f"(score={best_score:.2f})"
            )
            return best_id

        if best_id:
            LOGGER.info(
                f"Cinemeta low-confidence for '{title}' (year={year}, type={type_}) | "
                f"best '{best_title}' [{best_id}] score={best_score:.2f}"
            )
        else:
            LOGGER.info(f"Cinemeta no results for '{title}' (year={year}, type={type_})")
        return None

    return await cached_call(IMDB_CACHE, cache_key, "imdb_search", _produce)


async def cached_detail(imdb_id: str, media_type: str):
    async def _produce():
        return await get_detail(imdb_id=imdb_id, media_type=media_type)

    return await cached_call(IMDB_CACHE, imdb_id, "imdb_detail", _produce)


async def cached_season(imdb_id: str, season, episode):
    key = f"{imdb_id}::{season}::{episode}"

    async def _produce():
        return await get_season(imdb_id=imdb_id, season_id=season, episode_id=episode)

    return await cached_call(IMDB_CACHE, key, "imdb_season", _produce)


def build_movie_payload(imdb: dict, imdb_id: str, title: str, quality, encoded_string) -> dict:
    images = format_imdb_images(imdb_id)
    display = imdb.get("title", title) or title or ""
    payload = {
        "tmdb_id": imdb.get("moviedb_id") or (imdb_id.replace("tt", "") if imdb_id else None),
        "imdb_id": imdb_id,
        "title": display,
        "title_english": display,
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "poster": images["poster"] or imdb.get("poster") or "",
        "backdrop": images["backdrop"] or imdb.get("background") or "",
        "logo": images["logo"] or imdb.get("logo") or logo_from_imdb(imdb_id) or "",
        "cast": imdb.get("cast", []),
        "runtime": str(imdb.get("runtime") or ""),
        "media_type": "movie",
        "genres": imdb.get("genre", []),
        "quality": quality,
        "encoded_string": encoded_string,
    }
    return ensure_media_ids(payload, seed=f"cinemeta:{imdb_id}")


def build_tv_payload(imdb, ep, imdb_id, title, season, episode, quality, encoded_string) -> dict:
    images = format_imdb_images(imdb_id)
    display = imdb.get("title", title) or title or ""
    payload = {
        "tmdb_id": imdb.get("moviedb_id") or (imdb_id.replace("tt", "") if imdb_id else None),
        "imdb_id": imdb_id,
        "title": display,
        "title_english": display,
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "poster": images["poster"] or imdb.get("poster") or "",
        "backdrop": images["backdrop"] or imdb.get("background") or "",
        "logo": images["logo"] or imdb.get("logo") or logo_from_imdb(imdb_id) or "",
        "cast": imdb.get("cast", []),
        "runtime": str(imdb.get("runtime") or ""),
        "genres": imdb.get("genre", []),
        "media_type": "tv",
        "season_number": season,
        "episode_number": episode,
        "episode_title": (ep or {}).get("title", f"S{season:02d}E{episode:02d}"),
        "episode_backdrop": (ep or {}).get("image", ""),
        "episode_overview": (ep or {}).get("plot", ""),
        "episode_released": str((ep or {}).get("released", "")),
        "quality": quality,
        "encoded_string": encoded_string,
    }
    return ensure_media_ids(payload, seed=f"cinemeta:{imdb_id}")
