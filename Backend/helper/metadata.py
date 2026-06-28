import asyncio
import re
import traceback
from typing import Optional, List

import PTN

import Backend
from Backend.logger import LOGGER
from Backend.helper.imdb import get_detail, get_season, search_title, search_title_multi
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.encrypt import encode_string
from Backend.helper.split_files import parse_split_info, parse_combined_episodes
from Backend.helper.anime import fetch_anime_metadata, fetch_anime_movie_metadata
from themoviedb import aioTMDb
from rapidfuzz import fuzz
from guessit import guessit as _guessit
from difflib import SequenceMatcher

def _fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        set_ratio = fuzz.token_set_ratio(a, b) / 100.0
        sort_ratio = fuzz.token_sort_ratio(a, b) / 100.0
        a_tokens, b_tokens = a.split(), b.split()
        if a_tokens and b_tokens:
            coverage = min(len(a_tokens), len(b_tokens)) / max(len(a_tokens), len(b_tokens))
        else:
            coverage = 0.0
        return max(sort_ratio, set_ratio * coverage)
    except Exception:
        return SequenceMatcher(None, a, b).ratio()


_CINEMETA_THRESHOLD = 0.60
_TMDB_THRESHOLD = 0.55
_STRONG_MATCH = 0.92
_ALT_TITLE_LOOKUPS = 5

IMDB_CACHE: dict = {}
TMDB_SEARCH_CACHE: dict = {}
TMDB_DETAILS_CACHE: dict = {}
EPISODE_CACHE: dict = {}
ALT_TITLES_CACHE: dict = {}

API_SEMAPHORE = asyncio.Semaphore(12)

_MULTIPART_RE = re.compile(r"(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)", re.IGNORECASE)

# Combined episode files (e.g. "S01 [E04-06]" or "S01 Combined") are grouped under a special season.
COMBINED_SEASON = 0


# Re-file a combined entry under the special season with a descriptive title.
def _apply_combined_override(payload: dict, combined: dict) -> None:
    season, start, end = combined["season"], combined["start"], combined["end"]
    payload["season_number"] = COMBINED_SEASON
    if start is None:
        payload["episode_number"] = season * 1000
        payload["episode_title"] = f"S{season:02d} Complete (Combined)"
    else:
        payload["episode_number"] = season * 1000 + start
        payload["episode_title"] = f"S{season:02d} E{start:02d}-E{end:02d} (Combined)"
    if not payload.get("episode_backdrop"):
        payload["episode_backdrop"] = payload.get("backdrop") or payload.get("poster") or ""

_tmdb_client: aioTMDb | None = None
_tmdb_client_key: str | None = None


# Return a cached TMDb client, rebuilding it when the configured API key changes.
def get_tmdb_client() -> aioTMDb:
    global _tmdb_client, _tmdb_client_key
    current_key = SettingsManager.current().tmdb_api
    if _tmdb_client is None or _tmdb_client_key != current_key:
        _tmdb_client = aioTMDb(key=current_key, language="en-US", region="US")
        _tmdb_client_key = current_key
    return _tmdb_client


def format_tmdb_image(path: str, size="w500") -> str:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ""

def get_tmdb_logo(images) -> str:
    logos = getattr(images, "logos", None) if images else None
    if not logos:
        return ""
    for logo in logos:
        if getattr(logo, "iso_639_1", None) == "en" and getattr(logo, "file_path", None):
            return format_tmdb_image(logo.file_path, "w300")
    for logo in logos:
        if getattr(logo, "file_path", None):
            return format_tmdb_image(logo.file_path, "w300")
    return ""

def format_imdb_images(imdb_id: str) -> dict:
    if not imdb_id:
        return {"poster": "", "backdrop": "", "logo": ""}
    return {
        "poster": f"https://images.metahub.space/poster/small/{imdb_id}/img",
        "backdrop": f"https://images.metahub.space/background/medium/{imdb_id}/img",
        "logo": f"https://images.metahub.space/logo/medium/{imdb_id}/img",
    }


def extract_default_id(text: str) -> str | None:
    if not text:
        return None
    bare_imdb = re.search(r"\b(tt\d{7,10})\b", text)
    if bare_imdb:
        return bare_imdb.group(1)
    imdb_url = re.search(r"/title/(tt\d+)", text)
    if imdb_url:
        return imdb_url.group(1)
    tmdb_url = re.search(r"/(?:movie|tv)/(\d+)", text)
    if tmdb_url:
        return tmdb_url.group(1)
    return None

def _split_default_id(default_id) -> tuple[str | None, int | None, bool, bool]:
    if not default_id:
        return None, None, False, False
    value = str(default_id).strip()
    if value.startswith("tt"):
        return value, None, True, False
    if value.isdigit():
        return None, int(value), False, True
    return None, None, False, False

def _normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def _title_similarity(t1: str, t2: str) -> float:
    n1, n2 = _normalize_title(t1), _normalize_title(t2)
    if not n1 or not n2:
        return 0.0
    return _fuzzy_ratio(n1, n2)

def _year_from_str(year_val) -> int:
    if not year_val:
        return 0
    m = re.search(r"(\d{4})", str(year_val))
    return int(m.group(1)) if m else 0

def _score_candidate(
    query_title: str,
    query_year: Optional[int],
    result_title: str,
    result_year: int,
    year_reliable: bool = True,
) -> float:
    score = _title_similarity(query_title, result_title)
    
    if score < 0.5:
        return score

    if query_year and result_year:
        diff = abs(int(query_year) - result_year)
        if year_reliable:
            if diff > 2:
                score = max(0.0, score - 0.10 * (diff - 2))
            elif score >= 0.80:
                if diff == 0:
                    score = min(1.0, score + 0.20)
                elif diff == 1:
                    score = min(1.0, score + 0.07)
        else:
            if diff == 0 and score >= 0.80:
                score = min(1.0, score + 0.05)
    return score

def _build_query_variants(title: str, year: Optional[int] = None) -> List[str]:
    variants: List[str] = [title]
    if year:
        variants.append(f"{title} {year}")

    stripped = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title)).strip()
    if stripped and stripped.lower() != title.lower():
        variants.append(stripped)
        if year:
            variants.append(f"{stripped} {year}")

    no_article = re.sub(r"^\b(the|a|an)\b\s+", "", title, flags=re.IGNORECASE).strip()
    if no_article and no_article.lower() != title.lower():
        variants.append(no_article)

    seen: set = set()
    ordered: List[str] = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            ordered.append(v)
    return ordered

def _first(value):
    return value[0] if isinstance(value, list) else value

def parse_media_name(name: str) -> dict:
    try:
        ptn = PTN.parse(name) or {}
    except Exception as e:
        LOGGER.warning(f"PTN parsing failed for {name}: {e}")
        ptn = {}

    parsed = {
        "title": ptn.get("title"),
        "year": ptn.get("year"),
        "season": ptn.get("season"),
        "episode": ptn.get("episode"),
        "quality": ptn.get("resolution"),
        "excess": ptn.get("excess"),
    }

    if _guessit:
        try:
            g = _guessit(name)
            parsed["title"] = parsed["title"] or _first(g.get("title"))
            parsed["year"] = parsed["year"] or _first(g.get("year"))
            parsed["season"] = parsed["season"] or _first(g.get("season"))
            parsed["episode"] = parsed["episode"] or _first(g.get("episode"))
            parsed["quality"] = parsed["quality"] or _first(g.get("screen_size"))
        except Exception as e:
            LOGGER.warning(f"GuessIt parsing failed for {name}: {e}")

    return parsed

async def safe_imdb_search(title: str, type_: str, year: Optional[int] = None) -> str | None:
    cache_key = f"imdb::{type_}::{title}::{year}"
    if cache_key in IMDB_CACHE:
        return IMDB_CACHE[cache_key]

    query_variants = _build_query_variants(title, year)
    best_id: str | None = None
    best_score = 0.0
    best_title = ""

    year_reliable = type_ == "movie"

    for query in query_variants:
        try:
            async with API_SEMAPHORE:
                results = await search_title_multi(query=query, type=type_, limit=8)
            for r in results:
                score = _score_candidate(
                    title, year, r.get("title", ""), _year_from_str(r.get("year", "")),
                    year_reliable=year_reliable,
                )
                if score > best_score:
                    best_score, best_id, best_title = score, r.get("id"), r.get("title", "")
                if best_score >= _STRONG_MATCH:
                    break
        except Exception as e:
            LOGGER.warning(f"Cinemeta search variant '{query}' [{type_}] failed: {e}")
        if best_score >= _STRONG_MATCH:
            break

    if best_score >= _CINEMETA_THRESHOLD and best_id:
        LOGGER.info(f"Cinemeta match: '{title}' (year={year}) -> '{best_title}' [{best_id}] (score={best_score:.2f})")
        IMDB_CACHE[cache_key] = best_id
        return best_id

    if best_id:
        LOGGER.info(
            f"Cinemeta low-confidence for '{title}' (year={year}, type={type_}) | "
            f"best '{best_title}' [{best_id}] score={best_score:.2f} -> falling back to TMDb"
        )
    else:
        LOGGER.info(f"Cinemeta returned no results for '{title}' (year={year}, type={type_}) -> falling back to TMDb")

    IMDB_CACHE[cache_key] = None
    return None

async def _tmdb_raw_search(title: str, media_type: str, year: Optional[int]):
    client = get_tmdb_client()
    async with API_SEMAPHORE:
        if media_type == "movie":
            results = await (client.search().movies(query=title, year=year) if year else client.search().movies(query=title))
            if not results and year:
                results = await client.search().movies(query=title)
            return results
        return await client.search().tv(query=title)

async def safe_tmdb_search(title: str, type_: str, year: Optional[int] = None):
    cache_key = f"tmdb_search::{type_}::{title}::{year}"
    if cache_key in TMDB_SEARCH_CACHE:
        return TMDB_SEARCH_CACHE[cache_key]

    try:
        results = await _tmdb_raw_search(title, type_, year)
        best = await _pick_best_tmdb_result(results, title, year, type_)
        if best is None and results:
            top = results[0]
            top_title = getattr(top, "title" if type_ == "movie" else "name", "?")
            LOGGER.info(f"TMDb '{title}' (year={year}) top result '{top_title}' did not meet threshold")
        TMDB_SEARCH_CACHE[cache_key] = best
        return best
    except Exception as e:
        LOGGER.error(f"TMDb search failed for '{title}' [{type_}]: {e}")
        TMDB_SEARCH_CACHE[cache_key] = None
        return None

def _tmdb_title_year(item, media_type: str) -> tuple[str, int]:
    if media_type == "movie":
        date = getattr(item, "release_date", None)
        return getattr(item, "title", "") or "", getattr(date, "year", 0) if date else 0
    date = getattr(item, "first_air_date", None)
    return getattr(item, "name", "") or "", getattr(date, "year", 0) if date else 0

async def _pick_best_tmdb_result(results, query_title: str, query_year: Optional[int], media_type: str):
    if not results:
        return None

    year_reliable = media_type == "movie"

    scored = []
    best_item, best_score = None, 0.0
    for item in results:
        r_title, r_year = _tmdb_title_year(item, media_type)
        score = _score_candidate(query_title, query_year, r_title, r_year, year_reliable=year_reliable)
        scored.append((score, item, r_year))
        if score > best_score:
            best_score, best_item = score, item

    if best_score >= _STRONG_MATCH:
        return best_item

    scored.sort(key=lambda x: x[0], reverse=True)
    for _, item, r_year in scored[:_ALT_TITLE_LOOKUPS]:
        alt_titles = await _tmdb_alternative_titles(media_type, getattr(item, "id", None))
        for alt in alt_titles:
            alt_score = _score_candidate(query_title, query_year, alt, r_year, year_reliable=year_reliable)
            if alt_score > best_score:
                best_score, best_item = alt_score, item
                if best_score >= _STRONG_MATCH:
                    break
        if best_score >= _STRONG_MATCH:
            break

    return best_item if best_score >= _TMDB_THRESHOLD and best_item is not None else None

async def _tmdb_alternative_titles(media_type: str, tmdb_id) -> list[str]:
    if not tmdb_id:
        return []
    cache_key = (media_type, tmdb_id)
    if cache_key in ALT_TITLES_CACHE:
        return ALT_TITLES_CACHE[cache_key]
    titles: list[str] = []
    try:
        client = get_tmdb_client()
        async with API_SEMAPHORE:
            target = client.movie(tmdb_id) if media_type == "movie" else client.tv(tmdb_id)
            alt = await target.alternative_titles()
        entries = list(getattr(alt, "titles", None) or []) + list(getattr(alt, "results", None) or [])
        titles = [t for t in (getattr(e, "title", "") for e in entries) if t]
    except Exception as e:
        LOGGER.warning(f"TMDb alternative-titles fetch failed for {media_type} id={tmdb_id}: {e}")
    ALT_TITLES_CACHE[cache_key] = titles
    return titles

async def _tmdb_details(media_type: str, item_id):
    cache_key = (media_type, item_id)
    if cache_key in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[cache_key]
    try:
        client = get_tmdb_client()
        async with API_SEMAPHORE:
            target = client.movie(item_id) if media_type == "movie" else client.tv(item_id)
            details = await target.details(append_to_response="external_ids,credits")
            details.images = await target.images()
        TMDB_DETAILS_CACHE[cache_key] = details
        return details
    except Exception as e:
        LOGGER.warning(f"TMDb {media_type} details fetch failed for id={item_id}: {e}")
        TMDB_DETAILS_CACHE[cache_key] = None
        return None

async def _tmdb_episode_details(tv_id, season, episode):
    key = (tv_id, season, episode)
    if key in EPISODE_CACHE:
        return EPISODE_CACHE[key]
    try:
        async with API_SEMAPHORE:
            details = await get_tmdb_client().episode(tv_id, season, episode).details()
        EPISODE_CACHE[key] = details
        return details
    except Exception:
        EPISODE_CACHE[key] = None
        return None

async def _cached_imdb_detail(imdb_id: str, media_type: str):
    cached = IMDB_CACHE.get(imdb_id)
    if isinstance(cached, dict):
        return cached
    async with API_SEMAPHORE:
        detail = await get_detail(imdb_id=imdb_id, media_type=media_type)
    IMDB_CACHE[imdb_id] = detail
    return detail

async def _cached_imdb_season(imdb_id: str, season, episode):
    key = f"{imdb_id}::{season}::{episode}"
    if key in EPISODE_CACHE:
        return EPISODE_CACHE[key]
    async with API_SEMAPHORE:
        ep = await get_season(imdb_id=imdb_id, season_id=season, episode_id=episode)
    EPISODE_CACHE[key] = ep
    return ep

async def _tmdb_external_imdb_id(media_type: str, tmdb_id) -> str | None:
    try:
        details = await _tmdb_details(media_type, tmdb_id)
        ext = getattr(details, "external_ids", None) if details else None
        return getattr(ext, "imdb_id", None) if ext else None
    except Exception:
        return None



def _extract_cast(details) -> list:
    credits = getattr(details, "credits", None) or {}
    cast = getattr(credits, "cast", []) or []
    return [getattr(c, "name", None) or getattr(c, "original_name", None) for c in cast]


def _format_runtime(minutes) -> str:
    return f"{minutes} min" if minutes else ""


# Build the indexer movie payload from a TMDb details object.
def _build_tmdb_movie_payload(movie, quality, encoded_string) -> dict:
    release = getattr(movie, "release_date", None)
    return {
        "tmdb_id": movie.id,
        "imdb_id": getattr(getattr(movie, "external_ids", None), "imdb_id", None),
        "title": movie.title,
        "year": getattr(release, "year", 0) if release else 0,
        "rate": getattr(movie, "vote_average", 0) or 0,
        "description": movie.overview or "",
        "poster": format_tmdb_image(movie.poster_path),
        "backdrop": format_tmdb_image(movie.backdrop_path, "original"),
        "logo": get_tmdb_logo(getattr(movie, "images", None)),
        "cast": _extract_cast(movie),
        "runtime": str(_format_runtime(getattr(movie, "runtime", None))),
        "media_type": "movie",
        "genres": [g.name for g in (movie.genres or [])],
        "quality": quality,
        "encoded_string": encoded_string,
    }


# Build the indexer TV payload from TMDb series + episode details.
def _build_tmdb_tv_payload(tv, ep, season, episode, quality, encoded_string) -> dict:
    first_air = getattr(tv, "first_air_date", None)
    series_runtime = tv.episode_run_time[0] if getattr(tv, "episode_run_time", None) else None
    runtime = _format_runtime((getattr(ep, "runtime", None) if ep else None) or series_runtime)
    fallback_ep_title = f"S{season:02d}E{episode:02d}"
    return {
        "tmdb_id": tv.id,
        "imdb_id": getattr(getattr(tv, "external_ids", None), "imdb_id", None),
        "title": tv.name,
        "year": getattr(first_air, "year", 0) if first_air else 0,
        "rate": getattr(tv, "vote_average", 0) or 0,
        "description": tv.overview or "",
        "poster": format_tmdb_image(tv.poster_path),
        "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
        "logo": get_tmdb_logo(getattr(tv, "images", None)),
        "genres": [g.name for g in (tv.genres or [])],
        "media_type": "tv",
        "cast": _extract_cast(tv),
        "runtime": str(runtime),
        "season_number": season,
        "episode_number": episode,
        "episode_title": getattr(ep, "name", fallback_ep_title) if ep else fallback_ep_title,
        "episode_backdrop": format_tmdb_image(getattr(ep, "still_path", None), "original") if ep else "",
        "episode_overview": getattr(ep, "overview", "") if ep else "",
        "episode_released": ep.air_date.strftime("%Y-%m-%dT05:00:00.000Z") if (ep and getattr(ep, "air_date", None)) else "",
        "quality": quality,
        "encoded_string": encoded_string,
    }


# Build the indexer movie payload from Cinemeta/IMDb details.
def _build_imdb_movie_payload(imdb, imdb_id, title, quality, encoded_string) -> dict:
    images = format_imdb_images(imdb_id)
    return {
        "tmdb_id": imdb.get("moviedb_id") or (imdb_id.replace("tt", "") if imdb_id else None),
        "imdb_id": imdb_id,
        "title": imdb.get("title", title),
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "poster": images["poster"],
        "backdrop": images["backdrop"],
        "logo": images["logo"],
        "cast": imdb.get("cast", []),
        "runtime": str(imdb.get("runtime") or ""),
        "media_type": "movie",
        "genres": imdb.get("genre", []),
        "quality": quality,
        "encoded_string": encoded_string,
    }


# Build the indexer TV payload from Cinemeta/IMDb series + episode details.
def _build_imdb_tv_payload(imdb, ep, imdb_id, title, season, episode, quality, encoded_string) -> dict:
    images = format_imdb_images(imdb_id)
    return {
        "tmdb_id": imdb.get("moviedb_id") or (imdb_id.replace("tt", "") if imdb_id else None),
        "imdb_id": imdb_id,
        "title": imdb.get("title", title),
        "year": imdb.get("releaseDetailed", {}).get("year", 0),
        "rate": imdb.get("rating", {}).get("star", 0),
        "description": imdb.get("plot", ""),
        "poster": images["poster"],
        "backdrop": images["backdrop"],
        "logo": images["logo"],
        "cast": imdb.get("cast", []),
        "runtime": str(imdb.get("runtime") or ""),
        "genres": imdb.get("genre", []),
        "media_type": "tv",
        "season_number": season,
        "episode_number": episode,
        "episode_title": ep.get("title", f"S{season:02d}E{episode:02d}"),
        "episode_backdrop": ep.get("image", ""),
        "episode_overview": ep.get("plot", ""),
        "episode_released": str(ep.get("released", "")),
        "quality": quality,
        "encoded_string": encoded_string,
    }


# ----------------- Main entry-point -----------------

# True when a file's channel is configured as an anime channel.
def _is_anime_channel(channel) -> bool:
    anime_channels = SettingsManager.current().anime_channels
    if not anime_channels:
        return False
    target = str(channel).replace("-100", "")
    return any(str(c).strip().replace("-100", "") == target for c in anime_channels)


# Resolve anime TV metadata, filling the imdb_id from tmdb when ani.zip lacks it.
async def _fetch_anime_tv(title, season, episode, encoded_string, year, quality) -> dict | None:
    try:
        result = await fetch_anime_metadata(title, season, episode, encoded_string, year, quality)
    except Exception as e:
        LOGGER.warning(f"[ANIME] metadata error for '{title}': {e}")
        return None
    if result is None:
        return None
    if not result.get("imdb_id") and result.get("tmdb_id"):
        result["imdb_id"] = await _tmdb_external_imdb_id("tv", result["tmdb_id"])
    if not result.get("imdb_id"):
        LOGGER.info(f"[ANIME] No imdb id for '{title}' -> falling back to TMDb/Cinemeta")
        return None
    LOGGER.info(f"[ANIME] Matched '{result.get('title')}' [{result.get('imdb_id')}] S{season:02d}E{episode:02d}")
    return result


# Resolve anime movie metadata, filling the imdb_id from tmdb when ani.zip lacks it.
async def _fetch_anime_movie(title, encoded_string, year, quality) -> dict | None:
    try:
        result = await fetch_anime_movie_metadata(title, encoded_string, year, quality)
    except Exception as e:
        LOGGER.warning(f"[ANIME] movie metadata error for '{title}': {e}")
        return None
    if result is None:
        return None
    if not result.get("imdb_id") and result.get("tmdb_id"):
        result["imdb_id"] = await _tmdb_external_imdb_id("movie", result["tmdb_id"])
    if not result.get("imdb_id"):
        LOGGER.info(f"[ANIME] No imdb id for movie '{title}' -> falling back to TMDb/Cinemeta")
        return None
    LOGGER.info(f"[ANIME] Matched movie '{result.get('title')}' [{result.get('imdb_id')}]")
    return result


# Parse a filename/caption and resolve full movie or TV metadata for the indexer.
async def metadata(filename: str, channel: int, msg_id, override_id: str = None) -> dict | None:
    try:
        parsed = parse_media_name(filename)
    except Exception as e:
        LOGGER.error(f"Parsing failed for {filename}: {e}\n{traceback.format_exc()}")
        return None

    if _MULTIPART_RE.search(filename):
        LOGGER.info(f"Skipping {filename}: split video file not meant to be combined in Stremio")
        return None

    combined = parse_combined_episodes(filename)

    excess = parsed.get("excess")
    if not combined and excess and any("combined" in item.lower() for item in excess):
        LOGGER.info(f"Skipping {filename}: contains 'combined'")
        return None

    split_info = None if combined else parse_split_info(filename)
    part_number = split_info[1] if split_info else None

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    year = parsed.get("year")
    quality = parsed.get("quality")

    if combined:
        season, episode = combined["season"], combined["start"] or 1
    elif isinstance(season, list) or isinstance(episode, list):
        LOGGER.warning(f"Invalid season/episode format for {filename}: {parsed}")
        return None
    elif season and not episode:
        # Season pack with no episode number (e.g. "Season 01") -> whole-season combined.
        combined = {"season": season, "start": None, "end": None}
        episode = 1
    if not quality:
        LOGGER.warning(f"Skipping {filename}: No resolution (parsed={parsed})")
        return None
    if not title:
        LOGGER.info(f"No title parsed from: {filename} (parsed={parsed})")
        return None

    default_id = _resolve_default_id(override_id, filename)

    try:
        encoded_string = await encode_string({"chat_id": channel, "msg_id": msg_id})
    except Exception:
        encoded_string = None

    group_key = f"{channel}:{quality}:{split_info[0]}" if split_info else None

    try:
        if season and episode:
            LOGGER.info(f"Fetching TV metadata: {title} S{season:02d}E{episode:02d} (year={year})")
            result = None
            if not default_id and _is_anime_channel(channel):
                result = await _fetch_anime_tv(title, season, episode, encoded_string, year, quality)
            if result is None:
                result = await fetch_tv_metadata(title, season, episode, encoded_string, year, quality, default_id)
            if result is not None and combined:
                _apply_combined_override(result, combined)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} (year={year})")
            result = None
            if not default_id and _is_anime_channel(channel):
                result = await _fetch_anime_movie(title, encoded_string, year, quality)
            if result is None:
                result = await fetch_movie_metadata(title, encoded_string, year, quality, default_id)
        if result is not None:
            result["group_key"] = group_key
            result["part_number"] = part_number
        return result
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None


# Pick a default id from the override, the global setting, then the filename itself.
def _resolve_default_id(override_id, filename) -> str | None:
    for source in (override_id, getattr(Backend, "USE_DEFAULT_ID", None), filename):
        if not source:
            continue
        try:
            found = extract_default_id(source) or (override_id if source is override_id else None)
        except Exception:
            found = None
        if found:
            return found
    return None


# ----------------- TV metadata -----------------

# Resolve TV metadata, preferring Cinemeta and falling back to TMDb.
async def fetch_tv_metadata(title, season, episode, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id, tmdb_id, explicit_imdb_id, use_tmdb = _split_default_id(default_id)
    imdb_tv = None
    imdb_ep = None

    if not imdb_id and not tmdb_id:
        imdb_id = await safe_imdb_search(title, "tvSeries", year)
        use_tmdb = not bool(imdb_id)

    if imdb_id and not use_tmdb:
        try:
            imdb_tv = await _cached_imdb_detail(imdb_id, "tvSeries")
            imdb_ep = await _cached_imdb_season(imdb_id, season, episode)
        except Exception as e:
            LOGGER.warning(f"IMDb TV fetch failed [{imdb_id}] -> {e}")
            imdb_tv = imdb_ep = None
            use_tmdb = True

    # Guard against Cinemeta returning a wrong hit (skipped for user-supplied ids).
    if imdb_tv and not use_tmdb and not explicit_imdb_id:
        sim = _title_similarity(title, imdb_tv.get("title", ""))
        if sim < _CINEMETA_THRESHOLD:
            LOGGER.info(f"IMDb TV title mismatch for '{title}': got '{imdb_tv.get('title', '')}' (sim={sim:.2f}) -> TMDb")
            imdb_tv = None
            use_tmdb = True

    if use_tmdb or not imdb_tv:
        LOGGER.info(f"No valid Cinemeta TV data for '{title}' S{season:02d}E{episode:02d} -> using TMDb")
        if not tmdb_id:
            tmdb_search = await safe_tmdb_search(title, "tv", year) or (await safe_tmdb_search(title, "tv", None) if year else None)
            if not tmdb_search:
                LOGGER.info(f"No TMDb TV result for '{title}' S{season:02d}E{episode:02d} (year={year})")
                return None
            tmdb_id = tmdb_search.id

        tv = await _tmdb_details("tv", tmdb_id)
        if not tv:
            LOGGER.info(f"TMDb TV details failed for id={tmdb_id} ('{title}')")
            return None
        ep = await _tmdb_episode_details(tmdb_id, season, episode)
        return _build_tmdb_tv_payload(tv, ep, season, episode, quality, encoded_string)

    return _build_imdb_tv_payload(imdb_tv, imdb_ep or {}, imdb_id, title, season, episode, quality, encoded_string)


# ----------------- Movie metadata -----------------

# Resolve movie metadata, preferring Cinemeta and falling back to TMDb.
async def fetch_movie_metadata(title, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id, tmdb_id, explicit_imdb_id, use_tmdb = _split_default_id(default_id)
    imdb_details = None

    if not imdb_id and not tmdb_id:
        imdb_id = await safe_imdb_search(title, "movie", year)
        use_tmdb = not bool(imdb_id)

    if imdb_id and not use_tmdb:
        try:
            imdb_details = await _cached_imdb_detail(imdb_id, "movie")
        except Exception as e:
            LOGGER.warning(f"IMDb movie fetch failed [{title}] -> {e}")
            imdb_details = None
            use_tmdb = True

    # Guard against Cinemeta returning a wrong hit (skipped for user-supplied ids).
    if imdb_details and not use_tmdb and not explicit_imdb_id:
        sim = _title_similarity(title, imdb_details.get("title", ""))
        if sim < _CINEMETA_THRESHOLD:
            LOGGER.info(f"IMDb movie title mismatch for '{title}': got '{imdb_details.get('title', '')}' (sim={sim:.2f}) -> TMDb")
            imdb_details = None
            use_tmdb = True

    if use_tmdb or not imdb_details:
        LOGGER.info(f"No valid Cinemeta movie data for '{title}' (year={year}) -> using TMDb")
        if not tmdb_id:
            tmdb_result = await safe_tmdb_search(title, "movie", year) or (await safe_tmdb_search(title, "movie", None) if year else None)
            if not tmdb_result:
                LOGGER.info(f"No TMDb movie found for '{title}' (year={year})")
                return None
            tmdb_id = tmdb_result.id

        movie = await _tmdb_details("movie", tmdb_id)
        if not movie:
            LOGGER.info(f"TMDb movie details failed for id={tmdb_id} ('{title}')")
            return None
        return _build_tmdb_movie_payload(movie, quality, encoded_string)

    return _build_imdb_movie_payload(imdb_details, imdb_id, title, quality, encoded_string)


# ----------------- Candidate search (used by the /set command UI) -----------------

# Build a single candidate dict for the picker UI.
def _candidate_entry(source, title, year, imdb_id, tmdb_id, poster, backdrop, subtitle) -> dict:
    return {
        "source": source,
        "title": title or "",
        "year": year or "",
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "poster": poster,
        "backdrop": backdrop,
        "subtitle": subtitle,
    }


# Search IMDb + TMDb for picker candidates of the given media type.
async def _search_candidates(query: str, media_type: str, year: int | None = None, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    imdb_type = "movie" if media_type == "movie" else "tvSeries"
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        imdb_hit = await search_title(query=query, type=imdb_type)
        if imdb_hit and imdb_hit.get("id"):
            seen.add(("imdb", imdb_hit["id"]))
            results.append(_candidate_entry(
                "imdb", imdb_hit.get("title", ""), imdb_hit.get("year", ""),
                imdb_hit.get("id"), imdb_hit.get("moviedb_id"),
                imdb_hit.get("poster", ""), "", "IMDb / Cinemeta",
            ))
    except Exception as e:
        LOGGER.warning(f"IMDb {media_type} candidate search failed for '{query}': {e}")

    try:
        tmdb_results = await _tmdb_raw_search(query, media_type, year if media_type == "movie" else None)
        for item in (tmdb_results or [])[:limit]:
            tmdb_id = getattr(item, "id", None)
            if not tmdb_id or ("tmdb", str(tmdb_id)) in seen:
                continue
            seen.add(("tmdb", str(tmdb_id)))
            imdb_id = await _tmdb_external_imdb_id(media_type, tmdb_id)
            r_title, r_year = _tmdb_title_year(item, media_type)
            results.append(_candidate_entry(
                "tmdb", r_title, r_year or "", imdb_id, tmdb_id,
                format_tmdb_image(getattr(item, "poster_path", None)),
                format_tmdb_image(getattr(item, "backdrop_path", None), "original"),
                "TMDb",
            ))
    except Exception as e:
        LOGGER.warning(f"TMDb {media_type} candidate search failed for '{query}': {e}")

    return results[:limit]


# Search movie candidates for the picker UI.
async def search_movie_candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    return await _search_candidates(query, "movie", year, limit)


# Search TV candidates for the picker UI.
async def search_tv_candidates(query: str, limit: int = 8) -> list[dict]:
    return await _search_candidates(query, "tv", None, limit)


# ----------------- Manual /set command helpers -----------------

# Reshape an indexer payload into the manual-rescan response shape.
def _to_selection_payload(data: dict, media_type: str) -> dict:
    return {
        "tmdb_id": data.get("tmdb_id"),
        "imdb_id": data.get("imdb_id"),
        "title": data.get("title"),
        "release_year": data.get("year"),
        "rating": data.get("rate"),
        "description": data.get("description"),
        "poster": data.get("poster"),
        "backdrop": data.get("backdrop"),
        "logo": data.get("logo"),
        "genres": data.get("genres", []),
        "cast": data.get("cast", []),
        "runtime": data.get("runtime"),
        "media_type": media_type,
    }


# Fetch full movie metadata for a manually selected id.
async def fetch_selected_movie_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    if not selected_id:
        return None
    data = await fetch_movie_metadata(
        title="manual-rescan", encoded_string=None, year=None, quality=None, default_id=selected_id
    )
    return _to_selection_payload(data, "movie") if data else None


# Fetch full TV metadata for a manually selected id.
async def fetch_selected_tv_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    imdb_id, tmdb_id, _, use_tmdb = _split_default_id(selected_id)
    if not imdb_id and not tmdb_id:
        return None

    imdb_tv = None
    if imdb_id and not use_tmdb:
        try:
            imdb_tv = await get_detail(imdb_id=imdb_id, media_type="tvSeries")
        except Exception:
            imdb_tv = None
            use_tmdb = True

    if use_tmdb or not imdb_tv:
        if not tmdb_id and imdb_tv and imdb_tv.get("moviedb_id"):
            try:
                tmdb_id = int(imdb_tv["moviedb_id"])
            except Exception:
                tmdb_id = None
        if not tmdb_id:
            return None

        tv = await _tmdb_details("tv", tmdb_id)
        if not tv:
            return None
        first_air = getattr(tv, "first_air_date", None)
        runtime = _format_runtime(tv.episode_run_time[0] if getattr(tv, "episode_run_time", None) else None)
        return {
            "tmdb_id": tv.id,
            "imdb_id": getattr(getattr(tv, "external_ids", None), "imdb_id", None),
            "title": tv.name,
            "release_year": getattr(first_air, "year", 0) if first_air else 0,
            "rating": getattr(tv, "vote_average", 0) or 0,
            "description": tv.overview or "",
            "poster": format_tmdb_image(tv.poster_path),
            "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
            "logo": get_tmdb_logo(getattr(tv, "images", None)),
            "genres": [g.name for g in (tv.genres or [])],
            "cast": _extract_cast(tv),
            "runtime": str(runtime),
            "media_type": "tv",
        }

    images = format_imdb_images(imdb_id)
    return {
        "tmdb_id": int(imdb_tv.get("moviedb_id")) if imdb_tv.get("moviedb_id") else None,
        "imdb_id": imdb_id,
        "title": imdb_tv.get("title", ""),
        "release_year": imdb_tv.get("releaseDetailed", {}).get("year", 0),
        "rating": imdb_tv.get("rating", {}).get("star", 0),
        "description": imdb_tv.get("plot", ""),
        "poster": images["poster"],
        "backdrop": images["backdrop"],
        "logo": images["logo"],
        "genres": imdb_tv.get("genre", []),
        "cast": imdb_tv.get("cast", []),
        "runtime": str(imdb_tv.get("runtime") or ""),
        "media_type": "tv",
    }
