"""TheTVDB v4 metadata provider.

Requires a free TVDB API key (settings: tvdb_api).
Docs: https://thetvdb.github.io/v4-api/
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from Backend.helper.metadata.common import (
    ensure_media_ids,
    API_SEMAPHORE,
    STRONG_MATCH,
    TVDB_CACHE,
    TVDB_THRESHOLD,
    cached_call,
    logo_from_imdb,
    normalize_rating,
    parse_year_range,
    score_candidate_aliases,
    year_from_str,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

BASE = "https://api4.thetvdb.com/v4"
ARTWORK_BASE = "https://artworks.thetvdb.com"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_token: Optional[str] = None
_token_expires: float = 0.0


def tvdb_api_key() -> str:
    try:
        return str(SettingsManager.current().tvdb_api or "").strip()
    except Exception:
        return ""


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return _client


async def _ensure_token() -> Optional[str]:
    global _token, _token_expires
    key = tvdb_api_key()
    if not key:
        return None
    now = time.time()
    if _token and now < _token_expires - 60:
        return _token
    try:
        client = await _get_client()
        async with API_SEMAPHORE:
            resp = await client.post(f"{BASE}/login", json={"apikey": key})
        if resp.status_code != 200:
            LOGGER.warning(f"[TVDB] login failed: HTTP {resp.status_code}")
            return None
        data = resp.json() or {}
        _token = ((data.get("data") or {}).get("token")) or None
        # Tokens are valid ~1 month; refresh earlier
        _token_expires = now + 25 * 24 * 3600
        return _token
    except Exception as e:
        LOGGER.warning(f"[TVDB] login error: {e}")
        return None


async def _get(path: str, params: dict | None = None) -> Optional[dict]:
    token = await _ensure_token()
    if not token:
        return None
    try:
        client = await _get_client()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with API_SEMAPHORE:
            resp = await client.get(f"{BASE}{path}", params=params or {}, headers=headers)
        if resp.status_code == 401:
            # force re-login once
            global _token, _token_expires
            _token, _token_expires = None, 0.0
            token = await _ensure_token()
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            async with API_SEMAPHORE:
                resp = await client.get(f"{BASE}{path}", params=params or {}, headers=headers)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        LOGGER.warning(f"[TVDB] GET {path} failed: {e}")
        return None


def _art_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return f"{ARTWORK_BASE}{path}" if path.startswith("/") else f"{ARTWORK_BASE}/{path}"


def _pick_artwork(artworks: list, type_ids: set) -> str:
    for art in artworks or []:
        try:
            if int(art.get("type") or 0) in type_ids and art.get("image"):
                return _art_url(art["image"])
        except (TypeError, ValueError):
            continue
    return ""


async def search(title: str, year: Optional[int] = None, entity: str = "series") -> Optional[dict]:
    """Search TVDB and return best series/movie record or None."""
    if not tvdb_api_key():
        return None
    cache_key = f"tvdb_search::{entity}::{title}::{year}"

    async def _produce():
        q = title.strip()
        if not q:
            return None
        data = await _get("/search", {"query": q, "type": entity, "limit": 10})
        results = (data or {}).get("data") or []
        if not results:
            return None

        best, best_score = None, 0.0
        for r in results:
            r_title = r.get("name") or r.get("translations", {}).get("eng") or ""
            r_year = year_from_str(r.get("year") or r.get("first_air_time") or r.get("release_date"))
            # Search hits may include aliases / overviews / translations
            aliases = []
            for key in ("aliases", "alias", "primary_translated", "translations"):
                val = r.get(key)
                if not val:
                    continue
                if isinstance(val, dict):
                    aliases.extend(val.values())
                elif isinstance(val, list):
                    aliases.extend(val)
                else:
                    aliases.append(val)
            # Some TVDB search rows put extra names under overviews or slug-like fields
            if r.get("slug"):
                aliases.append(str(r["slug"]).replace("-", " "))
            score = score_candidate_aliases(
                title, year, r_title, r_year,
                aliases=aliases,
                year_reliable=(entity == "movie"),
                year_lower_bound=(entity == "series"),
            )
            if score > best_score:
                best_score, best = score, r

        # If still borderline, pull extended aliases for top candidates
        if best and best_score < STRONG_MATCH:
            ranked = []
            for r in results[:5]:
                r_title = r.get("name") or ""
                r_year = year_from_str(r.get("year") or r.get("first_air_time") or r.get("release_date"))
                aliases = r.get("aliases") or []
                sc = score_candidate_aliases(
                    title, year, r_title, r_year, aliases=aliases,
                    year_reliable=(entity == "movie"),
                    year_lower_bound=(entity == "series"),
                )
                ranked.append((sc, r))
            ranked.sort(key=lambda x: x[0], reverse=True)
            for sc, r in ranked[:3]:
                tvdb_id = r.get("tvdb_id") or r.get("id")
                try:
                    tvdb_id = int(tvdb_id)
                except (TypeError, ValueError):
                    continue
                ext = None
                try:
                    if entity == "series":
                        ext = await series_extended(tvdb_id)
                    else:
                        ext = await movie_extended(tvdb_id)
                except Exception:
                    ext = None
                if not ext:
                    continue
                ext_aliases = ext.get("aliases") or []
                ext_name = ext.get("name") or r.get("name") or ""
                ext_year = year_from_str(
                    ext.get("year") or ext.get("firstAired") or r.get("year")
                )
                # Also translations list if present
                for tr in (ext.get("translations") or {}).get("nameTranslations") or []:
                    if isinstance(tr, dict) and tr.get("name"):
                        ext_aliases = list(ext_aliases) + [tr["name"]]
                    elif isinstance(tr, str):
                        ext_aliases = list(ext_aliases) + [tr]
                sc2 = score_candidate_aliases(
                    title, year, ext_name, ext_year, aliases=ext_aliases,
                    year_reliable=(entity == "movie"),
                    year_lower_bound=(entity == "series"),
                )
                if sc2 > best_score:
                    best_score, best = sc2, r

        if best and best_score >= TVDB_THRESHOLD:
            LOGGER.info(
                f"[TVDB] match '{title}' -> '{best.get('name')}' "
                f"[{best.get('tvdb_id') or best.get('id')}] score={best_score:.2f}"
            )
            return best
        if best:
            LOGGER.info(
                f"[TVDB] low-confidence for '{title}': '{best.get('name')}' "
                f"score={best_score:.2f}"
            )
        return None

    return await cached_call(TVDB_CACHE, cache_key, "tvdb_search", _produce)


async def series_extended(tvdb_id: int) -> Optional[dict]:
    cache_key = f"tvdb_series::{tvdb_id}"

    async def _produce():
        data = await _get(f"/series/{tvdb_id}/extended")
        return (data or {}).get("data")

    return await cached_call(TVDB_CACHE, cache_key, "tvdb_series", _produce)


async def movie_extended(tvdb_id: int) -> Optional[dict]:
    cache_key = f"tvdb_movie::{tvdb_id}"

    async def _produce():
        data = await _get(f"/movies/{tvdb_id}/extended")
        return (data or {}).get("data")

    return await cached_call(TVDB_CACHE, cache_key, "tvdb_movie", _produce)


async def episode_by_number(tvdb_id: int, season: int, episode: int) -> Optional[dict]:
    cache_key = f"tvdb_ep::{tvdb_id}::{season}::{episode}"

    async def _produce():
        # Prefer seasons endpoint then filter
        data = await _get(
            f"/series/{tvdb_id}/episodes/default",
            {"page": 0},
        )
        episodes = (data or {}).get("data") or {}
        if isinstance(episodes, dict):
            episodes = episodes.get("episodes") or []
        for ep in episodes or []:
            try:
                if int(ep.get("seasonNumber") or -1) == int(season) and int(ep.get("number") or -1) == int(episode):
                    return ep
            except (TypeError, ValueError):
                continue
        # Fallback: search endpoint
        data = await _get(f"/series/{tvdb_id}/episodes/default", {"page": 0})
        return None

    return await cached_call(TVDB_CACHE, cache_key, "tvdb_ep", _produce)


async def _iter_series_episodes(tvdb_id: int, order: str = "default") -> list:
    """Page through TVDB series episodes (default or absolute order)."""
    all_eps: list = []
    page = 0
    while page < 40:  # hard safety cap
        data = await _get(
            f"/series/{tvdb_id}/episodes/{order}",
            {"page": page},
        )
        if not data:
            break
        block = (data.get("data") or {})
        if isinstance(block, dict):
            eps = block.get("episodes") or []
            links = block.get("links") or (data.get("links") or {})
        else:
            eps = block if isinstance(block, list) else []
            links = data.get("links") or {}
        if not eps:
            break
        all_eps.extend(eps)
        # pagination: TVDB v4 uses links.next
        next_url = None
        if isinstance(links, dict):
            next_url = links.get("next")
        if not next_url:
            break
        page += 1
    return all_eps


async def episode_by_absolute(tvdb_id: int, absolute: int) -> Optional[dict]:
    """Resolve an absolute episode number to a TVDB episode record (with S/E).

    Tries the absolute-order endpoint first, then falls back to scanning the
    default-order list for absoluteNumber / absoluteIndex fields.
    """
    cache_key = f"tvdb_abs::{tvdb_id}::{absolute}"

    async def _produce():
        abs_n = int(absolute)
        # 1) absolute order endpoint (episode.number == absolute)
        try:
            eps = await _iter_series_episodes(tvdb_id, order="absolute")
            for ep in eps:
                try:
                    if int(ep.get("number") or -1) == abs_n:
                        return ep
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            LOGGER.debug(f"[TVDB] absolute order fetch failed for {tvdb_id}: {e}")

        # 2) default order – match absoluteNumber / absoluteIndex
        try:
            eps = await _iter_series_episodes(tvdb_id, order="default")
            for ep in eps:
                for key in ("absoluteNumber", "absoluteIndex", "absNumber"):
                    try:
                        if int(ep.get(key) or -1) == abs_n:
                            return ep
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            LOGGER.debug(f"[TVDB] default order abs scan failed for {tvdb_id}: {e}")
        return None

    return await cached_call(TVDB_CACHE, cache_key, "tvdb_abs", _produce)


def _remote_ids(doc: dict) -> tuple:
    imdb_id = None
    tmdb_id = None
    for rid in (doc.get("remoteIds") or []):
        source = str(rid.get("sourceName") or rid.get("type") or "").lower()
        val = rid.get("id") or rid.get("value")
        if not val:
            continue
        if "imdb" in source:
            imdb_id = str(val)
            if not imdb_id.startswith("tt"):
                imdb_id = f"tt{imdb_id}" if imdb_id.isdigit() else imdb_id
        elif "themoviedb" in source or source == "tmdb":
            try:
                tmdb_id = int(val)
            except (TypeError, ValueError):
                pass
    return imdb_id, tmdb_id


def _genres(doc: dict) -> list:
    out = []
    for g in doc.get("genres") or []:
        name = g.get("name") if isinstance(g, dict) else str(g)
        if name:
            out.append(name)
    return out


def build_series_payload(
    series: dict,
    ep: Optional[dict],
    season: int,
    episode: int,
    quality,
    encoded_string,
) -> dict:
    imdb_id, tmdb_id = _remote_ids(series)
    artworks = series.get("artworks") or []
    poster = _pick_artwork(artworks, {2, 14, 27}) or _art_url(series.get("image") or "")
    backdrop = _pick_artwork(artworks, {3, 15, 19}) or _art_url(series.get("background") or "")
    logo = _pick_artwork(artworks, {25, 23}) or logo_from_imdb(imdb_id)
    year, year_end = parse_year_range(
        series.get("firstAired") or series.get("year"),
        series.get("lastAired") or series.get("nextAired"),
    )
    # TVDB `score` is popularity rank, not stars — prefer siteRating if present
    rate = normalize_rating(
        series.get("siteRating")
        or series.get("rating")
        or (series.get("score") if (series.get("score") or 0) <= 10 else 0)
    )
    fallback_ep = f"S{season:02d}E{episode:02d}"
    ep_title = (ep or {}).get("name") or fallback_ep
    ep_image = _art_url((ep or {}).get("image") or "")
    ep_overview = (ep or {}).get("overview") or ""
    ep_aired = (ep or {}).get("aired") or (ep or {}).get("firstAired") or ""
    title = series.get("name") or series.get("slug") or ""
    # Prefer English translation when available
    eng = None
    try:
        for tr in (series.get("translations") or {}).get("nameTranslations") or []:
            if isinstance(tr, dict) and (tr.get("language") or "").lower() in ("eng", "en"):
                eng = tr.get("name") or eng
    except Exception:
        pass
    payload = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": title,
        "title_english": eng or title,
        "original_title": series.get("name") or "",
        "year": year,
        "year_end": year_end,
        "rate": rate,
        "description": series.get("overview") or "",
        "poster": poster,
        "backdrop": backdrop,
        "logo": logo,
        "genres": _genres(series),
        "media_type": "tv",
        "cast": [],
        "runtime": "",
        "original_language": (series.get("originalLanguage") or None),
        "origin_country": list(series.get("originalCountry") or []) if isinstance(series.get("originalCountry"), list) else ([series["originalCountry"]] if series.get("originalCountry") else []),
        "season_number": season,
        "episode_number": episode,
        "episode_title": ep_title,
        "episode_backdrop": ep_image,
        "episode_overview": ep_overview,
        "episode_released": str(ep_aired),
        "quality": quality,
        "encoded_string": encoded_string,
        "tvdb_id": series.get("id"),
    }
    return ensure_media_ids(payload, seed=f"tvdb:{series.get('id')}")


def build_movie_payload(movie: dict, quality, encoded_string) -> dict:
    imdb_id, tmdb_id = _remote_ids(movie)
    artworks = movie.get("artworks") or []
    poster = _pick_artwork(artworks, {14, 2}) or _art_url(movie.get("image") or "")
    backdrop = _pick_artwork(artworks, {15, 3}) or ""
    logo = _pick_artwork(artworks, {25, 23}) or logo_from_imdb(imdb_id)
    year, year_end = parse_year_range(movie.get("year") or movie.get("releaseDate"), None)
    rate = normalize_rating(
        movie.get("siteRating")
        or movie.get("rating")
        or (movie.get("score") if (movie.get("score") or 0) <= 10 else 0)
    )
    runtime = movie.get("runtime")
    title = movie.get("name") or movie.get("slug") or ""
    payload = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": title,
        "title_english": title,
        "original_title": movie.get("name") or "",
        "year": year,
        "year_end": year_end,
        "rate": rate,
        "description": movie.get("overview") or "",
        "poster": poster,
        "backdrop": backdrop,
        "logo": logo,
        "genres": _genres(movie),
        "media_type": "movie",
        "cast": [],
        "runtime": f"{runtime} min" if runtime else "",
        "quality": quality,
        "encoded_string": encoded_string,
        "tvdb_id": movie.get("id"),
    }
    return ensure_media_ids(payload, seed=f"tvdb:{movie.get('id')}")


async def fetch_series_metadata(title, season, episode, encoded_string, year=None, quality=None) -> Optional[dict]:
    hit = await search(title, year=year, entity="series")
    if not hit:
        return None
    tvdb_id = hit.get("tvdb_id") or hit.get("id")
    try:
        tvdb_id = int(tvdb_id)
    except (TypeError, ValueError):
        return None
    series = await series_extended(tvdb_id)
    if not series:
        # fall back to search hit fields
        series = {
            "id": tvdb_id,
            "name": hit.get("name"),
            "overview": hit.get("overview"),
            "year": hit.get("year"),
            "image": hit.get("image_url") or hit.get("image"),
            "remoteIds": hit.get("remote_ids") or [],
        }
    ep = await episode_by_number(tvdb_id, season, episode)
    return build_series_payload(series, ep, season, episode, quality, encoded_string)


async def fetch_movie_metadata(title, encoded_string, year=None, quality=None) -> Optional[dict]:
    hit = await search(title, year=year, entity="movie")
    if not hit:
        return None
    tvdb_id = hit.get("tvdb_id") or hit.get("id")
    try:
        tvdb_id = int(tvdb_id)
    except (TypeError, ValueError):
        return None
    movie = await movie_extended(tvdb_id)
    if not movie:
        movie = {
            "id": tvdb_id,
            "name": hit.get("name"),
            "overview": hit.get("overview"),
            "year": hit.get("year"),
            "image": hit.get("image_url") or hit.get("image"),
            "remoteIds": hit.get("remote_ids") or [],
        }
    return build_movie_payload(movie, quality, encoded_string)
