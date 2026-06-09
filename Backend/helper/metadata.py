import asyncio
import traceback
import PTN
import re
from re import compile, IGNORECASE
from Backend.helper.imdb import get_detail, get_season, search_title, search_titles
from themoviedb import aioTMDb
from Backend.config import Telegram
import Backend
from Backend.logger import LOGGER
from Backend.helper.encrypt import encode_string
from Backend.helper.metadata_matcher import (
    MatchCandidate,
    MatchIntent,
    build_title_variants,
    choose_best_candidate,
    decision_metadata,
    normalize_title,
)

# ----------------- Configuration -----------------
DELAY = 0
tmdb = aioTMDb(key=Telegram.TMDB_API, language="en-US", region="US")

# Cache dictionaries (per run)
IMDB_CACHE: dict = {}
TMDB_SEARCH_CACHE: dict = {}
TMDB_DETAILS_CACHE: dict = {}
EPISODE_CACHE: dict = {}
MATCH_FAILURE_CACHE: dict = {}

# Concurrency semaphore for external API calls. Keep this conservative; bursts
# of forwarded episodes can otherwise cause TMDb connection resets.
API_SEMAPHORE = asyncio.Semaphore(4)

# ----------------- Helpers -----------------
def format_tmdb_image(path: str, size="w500") -> str:
    if not path:
        return ""
    return f"https://image.tmdb.org/t/p/{size}{path}"

def get_tmdb_logo(images) -> str:
    if not images:
        return ""
    logos = getattr(images, "logos", None)
    if not logos:
        return ""
    for logo in logos:
        iso_lang = getattr(logo, "iso_639_1", None)
        file_path = getattr(logo, "file_path", None)
        if iso_lang == "en" and file_path:
            return format_tmdb_image(file_path, "w300")
    for logo in logos:
        file_path = getattr(logo, "file_path", None)
        if file_path:
            return format_tmdb_image(file_path, "w300")
    return ""
    

def format_imdb_images(imdb_id: str) -> dict:
    if not imdb_id:
        return {"poster": "", "backdrop": "", "logo": ""}
    return {
        "poster": f"https://images.metahub.space/poster/small/{imdb_id}/img",
        "backdrop": f"https://images.metahub.space/background/medium/{imdb_id}/img",
        "logo": f"https://images.metahub.space/logo/medium/{imdb_id}/img",
    }

def extract_default_id(url: str) -> str | None:
    # IMDb
    imdb_match = re.search(r'/title/(tt\d+)', url)
    if imdb_match:
        return imdb_match.group(1)

    # TMDb movie or TV
    tmdb_match = re.search(r'/((movie|tv))/(\d+)', url)
    if tmdb_match:
        return tmdb_match.group(3)

    return None


def has_combined_marker(*values) -> bool:
    for value in values:
        if not value:
            continue
        if isinstance(value, dict):
            parts = value.values()
        elif isinstance(value, list):
            parts = value
        else:
            parts = [value]
        if any("combined" in str(item).lower() for item in parts):
            return True
    return False


def infer_resolution_from_filename(filename: str, parsed: dict) -> str:
    text = f"{filename} {parsed.get('quality', '')}".lower()

    # Strong signals first
    if any(tag in text for tag in ["2160", "4k", "uhd"]):
        return "2160p"
    if any(tag in text for tag in ["1080", "fhd"]):
        return "1080p"
    if "720" in text:
        return "720p"
    if "480" in text:
        return "480p"
    if "360" in text:
        return "360p"

    # If source/quality exists but no explicit resolution, keep it indexable
    if any(tag in text for tag in [
        "hdrip", "webrip", "web-dl", "hdtv", "dvdrip", "brrip", "bluray", "cam", "hdts", "hdtc"
    ]):
        return "480p"

    return "SD"


async def safe_imdb_search(title: str, type_: str) -> str | None:
    key = f"imdb::{type_}::{title}"
    if key in IMDB_CACHE:
        return IMDB_CACHE[key]
    try:
        async with API_SEMAPHORE:
            result = await search_title(query=title, type=type_)
        imdb_id = result["id"] if result else None
        IMDB_CACHE[key] = imdb_id
        return imdb_id
    except Exception as e:
        LOGGER.warning(f"IMDb search failed for '{title}' [{type_}]: {e}")
        return None

async def safe_tmdb_search(title: str, type_: str, year=None):
    key = f"tmdb_search::{type_}::{title}::{year}"
    if key in TMDB_SEARCH_CACHE:
        return TMDB_SEARCH_CACHE[key]
    try:
        async with API_SEMAPHORE:
            if type_ == "movie":
                results = await tmdb.search().movies(query=title, year=year) if year else await tmdb.search().movies(query=title)
            else:
                results = await tmdb.search().tv(query=title)
        res = results[0] if results else None
        TMDB_SEARCH_CACHE[key] = res
        return res
    except Exception as e:
        LOGGER.error(f"TMDb search failed for '{title}' [{type_}]: {e}")
        return None


def _cache_key(channel, msg_id) -> str:
    return f"{channel}:{msg_id}"


def set_match_failure(channel, msg_id, details: dict) -> None:
    MATCH_FAILURE_CACHE[_cache_key(channel, msg_id)] = details


def pop_match_failure(channel, msg_id) -> dict:
    return MATCH_FAILURE_CACHE.pop(_cache_key(channel, msg_id), {})


def _safe_year(value) -> int | None:
    try:
        value = int(value)
        return value if 1800 <= value <= 2200 else None
    except Exception:
        return None


def _tmdb_year(item, attr: str) -> int | None:
    value = getattr(item, attr, None)
    return _safe_year(getattr(value, "year", None)) if value else None


def _imdb_candidate(result: dict | None, media_type: str) -> MatchCandidate | None:
    if not result or not result.get("id"):
        return None
    return MatchCandidate(
        source="imdb",
        title=result.get("title") or "",
        year=_safe_year(result.get("year")),
        media_type=media_type,
        imdb_id=result.get("id"),
        tmdb_id=result.get("moviedb_id"),
        popularity=0.0,
        raw=result,
    )


def _dedupe_candidates(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    out: list[MatchCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.imdb_id:
            key = ("imdb", str(candidate.imdb_id))
        elif candidate.tmdb_id:
            key = ("tmdb", str(candidate.tmdb_id))
        else:
            key = (candidate.media_type, f"{normalize_title(candidate.title)}::{candidate.year}")
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


async def _database_candidates(variants: list[str], media_type: str, year=None, limit: int = 8) -> list[MatchCandidate]:
    candidates: list[MatchCandidate] = []
    try:
        if not getattr(Backend.db, "dbs", None):
            return []
        collection = "movie" if media_type == "movie" else "tv"
        for db_key in sorted(k for k in Backend.db.dbs if k.startswith("storage_")):
            db = Backend.db.dbs[db_key]
            for variant in variants:
                if not variant:
                    continue
                query = {"title": {"$regex": f"^{re.escape(variant)}$", "$options": "i"}}
                if year:
                    query["release_year"] = int(year)
                async for doc in db[collection].find(query).limit(limit):
                    candidates.append(MatchCandidate(
                        source="database",
                        title=doc.get("title") or "",
                        year=_safe_year(doc.get("release_year")),
                        media_type=media_type,
                        imdb_id=doc.get("imdb_id"),
                        tmdb_id=doc.get("tmdb_id"),
                        popularity=20.0,
                        raw={"db_key": db_key, "db_index": doc.get("db_index")},
                    ))
                if len(candidates) >= limit:
                    return candidates[:limit]
    except Exception as e:
        LOGGER.debug(f"Database metadata candidate lookup failed: {e}")
    return candidates[:limit]


async def _movie_candidates(variants: list[str], year=None, limit: int = 8) -> list[MatchCandidate]:
    candidates: list[MatchCandidate] = []
    candidates.extend(await _database_candidates(variants, "movie", year, limit=limit))

    for title in variants:
        try:
            async with API_SEMAPHORE:
                imdb_results = await search_titles(query=title, type="movie", limit=limit)
            for result in imdb_results:
                imdb_candidate = _imdb_candidate(result, "movie")
                if imdb_candidate:
                    candidates.append(imdb_candidate)
        except Exception as e:
            LOGGER.warning(f"IMDb movie candidate lookup failed for '{title}': {e}")

        try:
            async with API_SEMAPHORE:
                tmdb_results = await tmdb.search().movies(query=title, year=year) if year else await tmdb.search().movies(query=title)
            if not tmdb_results and year:
                async with API_SEMAPHORE:
                    tmdb_results = await tmdb.search().movies(query=title)
            for item in (tmdb_results or [])[:limit]:
                tmdb_id = getattr(item, "id", None)
                if not tmdb_id:
                    continue
                candidates.append(MatchCandidate(
                    source="tmdb",
                    title=getattr(item, "title", "") or "",
                    year=_tmdb_year(item, "release_date"),
                    media_type="movie",
                    tmdb_id=tmdb_id,
                    popularity=float(getattr(item, "popularity", 0.0) or 0.0),
                    raw=item,
                ))
        except Exception as e:
            LOGGER.warning(f"TMDb movie candidate lookup failed for '{title}': {e}")
    return _dedupe_candidates(candidates)


async def _tv_candidates(variants: list[str], year=None, limit: int = 8) -> list[MatchCandidate]:
    candidates: list[MatchCandidate] = []
    candidates.extend(await _database_candidates(variants, "tv", year, limit=limit))

    for title in variants:
        try:
            async with API_SEMAPHORE:
                imdb_results = await search_titles(query=title, type="tvSeries", limit=limit)
            for result in imdb_results:
                imdb_candidate = _imdb_candidate(result, "tv")
                if imdb_candidate:
                    candidates.append(imdb_candidate)
        except Exception as e:
            LOGGER.warning(f"IMDb TV candidate lookup failed for '{title}': {e}")

        try:
            async with API_SEMAPHORE:
                tmdb_results = await tmdb.search().tv(query=title)
            for item in (tmdb_results or [])[:limit]:
                tmdb_id = getattr(item, "id", None)
                if not tmdb_id:
                    continue
                candidates.append(MatchCandidate(
                    source="tmdb",
                    title=getattr(item, "name", "") or "",
                    year=_tmdb_year(item, "first_air_date"),
                    media_type="tv",
                    tmdb_id=tmdb_id,
                    popularity=float(getattr(item, "popularity", 0.0) or 0.0),
                    raw=item,
                ))
        except Exception as e:
            LOGGER.warning(f"TMDb TV candidate lookup failed for '{title}': {e}")
    return _dedupe_candidates(candidates)


async def resolve_movie_match(title: str, year=None, raw_title: str | None = None, parsed: dict | None = None) -> tuple[MatchCandidate | None, dict]:
    variants = build_title_variants(
        raw_title=raw_title or title,
        parsed_title=title,
        year=_safe_year(year),
        site=(parsed or {}).get("site"),
    )
    variants = variants or [normalize_title(title)]
    intent = MatchIntent(
        raw_title=title,
        clean_title=variants[0],
        year=_safe_year(year),
        media_type="movie",
        title_variants=variants,
    )
    decision = choose_best_candidate(intent, await _movie_candidates(variants, year))
    return decision.candidate if decision.accepted else None, decision_metadata(decision, intent)


async def resolve_tv_match(title: str, season=None, episode=None, year=None, season_pack: bool = False, raw_title: str | None = None, parsed: dict | None = None) -> tuple[MatchCandidate | None, dict]:
    variants = build_title_variants(
        raw_title=raw_title or title,
        parsed_title=title,
        year=_safe_year(year),
        site=(parsed or {}).get("site"),
    )
    variants = variants or [normalize_title(title)]
    intent = MatchIntent(
        raw_title=title,
        clean_title=variants[0],
        year=_safe_year(year),
        media_type="tv",
        title_variants=variants,
        season=season,
        episode=episode,
        season_pack=season_pack,
    )
    decision = choose_best_candidate(intent, await _tv_candidates(variants, year))
    return decision.candidate if decision.accepted else None, decision_metadata(decision, intent)


async def _retry_api_call(label: str, func, attempts: int = 3, base_delay: float = 0.8):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as e:
            last_error = e
            if attempt < attempts:
                await asyncio.sleep(base_delay * attempt)
    LOGGER.warning(f"{label} failed after {attempts} attempts: {last_error}")
    return None

async def _tmdb_movie_details(movie_id):
    if movie_id in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[movie_id]

    async def fetch():
        async with API_SEMAPHORE:
            details = await tmdb.movie(movie_id).details(
                append_to_response="external_ids,credits"
            )
            images = await tmdb.movie(movie_id).images()
            details.images = images
            return details

    details = await _retry_api_call(f"TMDb movie details fetch id={movie_id}", fetch)
    if details:
        TMDB_DETAILS_CACHE[movie_id] = details
        return details
    return None


async def _tmdb_tv_details(tv_id):
    if tv_id in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[tv_id]

    async def fetch():
        async with API_SEMAPHORE:
            details = await tmdb.tv(tv_id).details(
                append_to_response="external_ids,credits"
            )
            images = await tmdb.tv(tv_id).images()
            details.images = images
            return details

    details = await _retry_api_call(f"TMDb tv details fetch id={tv_id}", fetch)
    if details:
        TMDB_DETAILS_CACHE[tv_id] = details
        return details
    return None


async def _tmdb_episode_details(tv_id, season, episode):
    key = (tv_id, season, episode)
    if key in EPISODE_CACHE:
        return EPISODE_CACHE[key]

    async def fetch():
        async with API_SEMAPHORE:
            return await tmdb.episode(tv_id, season, episode).details()

    details = await _retry_api_call(f"TMDb episode details fetch id={tv_id} S{season}E{episode}", fetch, attempts=2)
    if details:
        EPISODE_CACHE[key] = details
        return details
    return None


def _year_from_date(value) -> int:
    try:
        return int(getattr(value, "year", 0) or 0)
    except Exception:
        return 0


def _date_to_iso(value) -> str:
    try:
        return value.strftime("%Y-%m-%dT05:00:00.000Z") if value else ""
    except Exception:
        return ""


def _tmdb_tv_fallback(search_result, title, season, episode, encoded_string, quality) -> dict:
    return {
        "tmdb_id": getattr(search_result, "id", None),
        "imdb_id": None,
        "title": getattr(search_result, "name", None) or title,
        "year": _year_from_date(getattr(search_result, "first_air_date", None)),
        "rate": getattr(search_result, "vote_average", 0) or 0,
        "description": getattr(search_result, "overview", "") or "",
        "poster": format_tmdb_image(getattr(search_result, "poster_path", None)),
        "backdrop": format_tmdb_image(getattr(search_result, "backdrop_path", None), "original"),
        "logo": "",
        "genres": [],
        "media_type": "tv",
        "cast": [],
        "runtime": "",
        "season_number": season,
        "episode_number": episode,
        "episode_title": f"S{season}E{episode}",
        "episode_backdrop": "",
        "episode_overview": "",
        "episode_released": "",
        "quality": quality,
        "encoded_string": encoded_string,
    }


def _tmdb_movie_fallback(search_result, title, encoded_string, year, quality) -> dict:
    return {
        "tmdb_id": getattr(search_result, "id", None),
        "imdb_id": None,
        "title": getattr(search_result, "title", None) or getattr(search_result, "name", None) or title,
        "year": _year_from_date(getattr(search_result, "release_date", None)) or (year or 0),
        "rate": getattr(search_result, "vote_average", 0) or 0,
        "description": getattr(search_result, "overview", "") or "",
        "poster": format_tmdb_image(getattr(search_result, "poster_path", None)),
        "backdrop": format_tmdb_image(getattr(search_result, "backdrop_path", None), "original"),
        "logo": "",
        "cast": [],
        "runtime": "",
        "media_type": "movie",
        "genres": [],
        "quality": quality,
        "encoded_string": encoded_string,
    }

# ----------------- Main Metadata -----------------
async def metadata(filename: str, channel: int, msg_id, override_id: str = None) -> dict | None:
    try:
        parsed = PTN.parse(filename)
    except Exception as e:
        LOGGER.error(f"PTN parsing failed for {filename}: {e}\n{traceback.format_exc()}")
        return None

    # Skip split/multipart files
    # if Telegram.SKIP_MULTIPART:
    multipart_pattern = compile(r'(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)', IGNORECASE)
    if multipart_pattern.search(filename):
        LOGGER.info(f"Skipping {filename}: seems to be a split/multipart file")
        return None

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    year = parsed.get("year")
    quality = parsed.get("resolution") or infer_resolution_from_filename(filename, parsed)
    if isinstance(season, list) or isinstance(episode, list):
        LOGGER.warning(f"Invalid season/episode format for {filename}: {parsed}")
        return None
    if season and not episode:
        is_season_pack = has_combined_marker(filename, parsed)
        if not is_season_pack:
            LOGGER.warning(f"Missing episode in {filename}: {parsed}")
            return None
    if not quality:
        quality = "SD"

    if not parsed.get("resolution"):
        LOGGER.info(f"No explicit resolution in {filename}; inferred quality={quality}")
    if not title:
        LOGGER.info(f"No title parsed from: {filename} (parsed={parsed})")
        return None


    default_id = None
    if override_id:
        try:
            default_id = extract_default_id(override_id) or override_id
        except Exception:
            pass
            
    if not default_id:
        try:
            default_id = extract_default_id(Backend.USE_DEFAULT_ID)
        except Exception:
            pass
            
    if not default_id:
        try:
            default_id = extract_default_id(filename)
        except Exception:
            pass

    explicit_default_id = bool(default_id)

    data = {"chat_id": channel, "msg_id": msg_id}
    try:
        encoded_string = await encode_string(data)
    except Exception:
        encoded_string = None

    try:
        if season and episode:
            LOGGER.info(f"Fetching TV metadata: {title} S{season}E{episode}")
            if not explicit_default_id and not default_id:
                candidate, match_details = await resolve_tv_match(title, season, episode, year, raw_title=filename, parsed=parsed)
                if not candidate:
                    set_match_failure(channel, msg_id, match_details)
                    LOGGER.warning(f"Strict TV metadata match rejected for {filename}: {match_details.get('match_rejection_reason')}")
                    return None
                default_id = candidate.imdb_id or candidate.tmdb_id
            return await fetch_tv_metadata(title, season, episode, encoded_string, year, quality, default_id)
        elif season:
            LOGGER.info(f"Fetching TV season-pack metadata: {title} S{season}")
            if not explicit_default_id and not default_id:
                candidate, match_details = await resolve_tv_match(title, season, None, year, season_pack=True, raw_title=filename, parsed=parsed)
                if not candidate:
                    set_match_failure(channel, msg_id, match_details)
                    LOGGER.warning(f"Strict TV season-pack metadata match rejected for {filename}: {match_details.get('match_rejection_reason')}")
                    return None
                default_id = candidate.imdb_id or candidate.tmdb_id
            return await fetch_tv_season_pack_metadata(title, season, encoded_string, year, quality, default_id)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} ({year})")
            if not explicit_default_id and not default_id:
                candidate, match_details = await resolve_movie_match(title, year, raw_title=filename, parsed=parsed)
                if not candidate:
                    set_match_failure(channel, msg_id, match_details)
                    LOGGER.warning(f"Strict movie metadata match rejected for {filename}: {match_details.get('match_rejection_reason')}")
                    return None
                default_id = candidate.imdb_id or candidate.tmdb_id
            return await fetch_movie_metadata(title, encoded_string, year, quality, default_id)
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None

# ----------------- TV Metadata -----------------
async def _get_tmdb_tv_id(title, year=None, default_id=None):
    if default_id:
        default_id = str(default_id).strip()
        if default_id.isdigit():
            return int(default_id)

    tmdb_search = await safe_tmdb_search(title, "tv", year)
    return tmdb_search.id if tmdb_search else None


async def _get_season_episode_count(title, season, year=None, default_id=None) -> int:
    try:
        tmdb_id = await _get_tmdb_tv_id(title, year, default_id)
        if not tmdb_id:
            return 1

        tv = await _tmdb_tv_details(tmdb_id)
        if not tv:
            return 1
        for season_info in getattr(tv, "seasons", []) or []:
            if int(getattr(season_info, "season_number", -1) or -1) == int(season):
                count = int(getattr(season_info, "episode_count", 0) or 0)
                return max(1, count)
    except Exception as e:
        LOGGER.warning(f"Could not determine episode count for {title} S{season}: {e}")

    return 1


async def fetch_tv_season_pack_metadata(title, season, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    base = await fetch_tv_metadata(title, season, 1, encoded_string, year, quality, default_id)
    if not base:
        return None

    episode_count = await _get_season_episode_count(title, season, year, default_id)
    tmdb_id = base.get("tmdb_id")
    episodes = []

    for episode_number in range(1, episode_count + 1):
        ep_details = None
        if tmdb_id:
            try:
                ep_details = await _tmdb_episode_details(int(tmdb_id), season, episode_number)
            except Exception:
                ep_details = None

        episodes.append(
            {
                "episode_number": episode_number,
                "episode_title": getattr(ep_details, "name", f"Episode {episode_number}") if ep_details else f"Episode {episode_number}",
                "episode_backdrop": format_tmdb_image(getattr(ep_details, "still_path", None), "original") if ep_details else "",
                "episode_overview": getattr(ep_details, "overview", "") if ep_details else "",
                "episode_released": (
                    ep_details.air_date.strftime("%Y-%m-%dT05:00:00.000Z")
                    if getattr(ep_details, "air_date", None)
                    else ""
                ),
            }
        )

    base.update(
        {
            "season_pack": True,
            "season_pack_episode_count": episode_count,
            "season_pack_episodes": episodes,
            "episode_number": 1,
            "episode_title": f"Season {int(season):02d} Pack",
        }
    )
    return base


async def fetch_tv_metadata(title, season, episode, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id = None
    tmdb_id = None
    imdb_tv = None
    imdb_ep = None
    use_tmdb = False

    # -------------------------------------------------------
    # 1. Handle default ID (IMDb / TMDb)
    # -------------------------------------------------------
    if default_id:
        default_id = str(default_id)
        if default_id.startswith("tt"):
            imdb_id = default_id
            use_tmdb = False
        elif default_id.isdigit():
            tmdb_id = int(default_id)
            use_tmdb = True

    # -------------------------------------------------------
    # 2. If no ID → Try IMDb search first
    # -------------------------------------------------------
    if not imdb_id and not tmdb_id:
        imdb_id = await safe_imdb_search(title, "tvSeries")
        use_tmdb = not bool(imdb_id)

    # -------------------------------------------------------
    # 3. IMDb fetch (series + episode)
    # -------------------------------------------------------
    if imdb_id and not use_tmdb:
        try:
            # ----- series details
            if imdb_id in IMDB_CACHE:
                imdb_tv = IMDB_CACHE[imdb_id]
            else:
                async with API_SEMAPHORE:
                    imdb_tv = await get_detail(imdb_id=imdb_id, media_type="tvSeries")
                IMDB_CACHE[imdb_id] = imdb_tv

            # ----- episode details
            ep_key = f"{imdb_id}::{season}::{episode}"
            if ep_key in EPISODE_CACHE:
                imdb_ep = EPISODE_CACHE[ep_key]
            else:
                async with API_SEMAPHORE:
                    imdb_ep = await get_season(imdb_id=imdb_id, season_id=season, episode_id=episode)
                EPISODE_CACHE[ep_key] = imdb_ep

        except Exception as e:
            LOGGER.warning(f"IMDb TV fetch failed [{imdb_id}] → {e}")
            imdb_tv = None
            imdb_ep = None
            use_tmdb = True

    # -------------------------------------------------------
    # 4. Decide if TMDb required
    # -------------------------------------------------------
    must_use_tmdb = (
        use_tmdb or
        imdb_tv is None or
        imdb_tv == {}
    )

    # =======================================================
    #  5. TMDb MODE
    # =======================================================
    if must_use_tmdb:
        LOGGER.info(f"No valid IMDb TV data for '{title}' → using TMDb")
        tmdb_search = None

        # Search TMDb by title
        if not tmdb_id:
            tmdb_search = await safe_tmdb_search(title, "tv", year)
            if not tmdb_search:
                LOGGER.warning(f"No TMDb TV result for '{title}'")
                return None
            tmdb_id = tmdb_search.id

        # Fetch full TV show details
        tv = await _tmdb_tv_details(tmdb_id)
        if not tv:
            LOGGER.warning(f"TMDb TV details failed for id={tmdb_id}")
            if tmdb_search:
                return _tmdb_tv_fallback(tmdb_search, title, season, episode, encoded_string, quality)
            return {
                "tmdb_id": tmdb_id,
                "imdb_id": None,
                "title": title,
                "year": year or 0,
                "rate": 0,
                "description": "",
                "poster": "",
                "backdrop": "",
                "logo": "",
                "genres": [],
                "media_type": "tv",
                "cast": [],
                "runtime": "",
                "season_number": season,
                "episode_number": episode,
                "episode_title": f"S{season}E{episode}",
                "episode_backdrop": "",
                "episode_overview": "",
                "episode_released": "",
                "quality": quality,
                "encoded_string": encoded_string,
            }

        # Fetch episode
        ep = await _tmdb_episode_details(tmdb_id, season, episode)

        # Cast list
        credits = getattr(tv, "credits", None) or {}
        cast_arr = getattr(credits, "cast", []) or []
        cast = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in cast_arr
        ]

        # Runtime (prefer episode → series → empty)
        ep_runtime = getattr(ep, "runtime", None) if ep else None
        series_runtime = (
            tv.episode_run_time[0] if getattr(tv, "episode_run_time", None) else None
        )
        runtime_val = ep_runtime or series_runtime
        runtime = f"{runtime_val} min" if runtime_val else ""

        return {
            "tmdb_id": tv.id,
            "imdb_id": getattr(getattr(tv, "external_ids", None), "imdb_id", None),
            "title": tv.name,
            "year": getattr(tv.first_air_date, "year", 0) if getattr(tv, "first_air_date", None) else 0,
            "rate": getattr(tv, "vote_average", 0) or 0,
            "description": tv.overview or "",
            "poster": format_tmdb_image(tv.poster_path),
            "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
            "logo": get_tmdb_logo(getattr(tv, "images", None)),
            "genres": [g.name for g in (tv.genres or [])],
            "media_type": "tv",
            "cast": cast,
            "runtime": str(runtime),

            "season_number": season,
            "episode_number": episode,
            "episode_title": getattr(ep, "name", f"S{season}E{episode}") if ep else f"S{season}E{episode}",
            "episode_backdrop": format_tmdb_image(getattr(ep, "still_path", None), "original") if ep else "",
            "episode_overview": getattr(ep, "overview", "") if ep else "",
            "episode_released": _date_to_iso(getattr(ep, "air_date", None)),

            "quality": quality,
            "encoded_string": encoded_string,
        }

    # =======================================================
    #  6. IMDb MODE
    # =======================================================
    imdb = imdb_tv or {}
    ep = imdb_ep or {}

    images = format_imdb_images(imdb_id)

    return {
        "tmdb_id": imdb.get("moviedb_id") or imdb_id.replace("tt", ""),
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
        "episode_title": ep.get("title", f"S{season}E{episode}"),
        "episode_backdrop": ep.get("image", ""),
        "episode_overview": ep.get("plot", ""),
        "episode_released": str(ep.get("released", "")),

        "quality": quality,
        "encoded_string": encoded_string,
    }


async def search_movie_candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        imdb_result = await search_title(query=query, type="movie")
        if imdb_result and imdb_result.get("id"):
            key = ("imdb", imdb_result["id"])
            if key not in seen:
                seen.add(key)
                results.append({
                    "source": "imdb",
                    "title": imdb_result.get("title", ""),
                    "year": imdb_result.get("year", ""),
                    "imdb_id": imdb_result.get("id"),
                    "tmdb_id": imdb_result.get("moviedb_id"),
                    "poster": imdb_result.get("poster", ""),
                    "backdrop": "",
                    "subtitle": "IMDb / Cinemeta",
                })
    except Exception as e:
        LOGGER.warning(f"IMDb movie candidate search failed for '{query}': {e}")

    try:
        async with API_SEMAPHORE:
            tmdb_results = await tmdb.search().movies(query=query, year=year) if year else await tmdb.search().movies(query=query)

        for item in (tmdb_results or [])[:limit]:
            tmdb_id = getattr(item, "id", None)
            if not tmdb_id:
                continue

            imdb_id = None
            try:
                details = await _tmdb_movie_details(tmdb_id)
                ext = getattr(details, "external_ids", None) if details else None
                imdb_id = getattr(ext, "imdb_id", None) if ext else None
            except Exception:
                pass

            key = ("tmdb", str(tmdb_id))
            if key in seen:
                continue
            seen.add(key)

            release_date = getattr(item, "release_date", None)
            year_value = getattr(release_date, "year", None) if release_date else None

            results.append({
                "source": "tmdb",
                "title": getattr(item, "title", "") or "",
                "year": year_value or "",
                "imdb_id": imdb_id,
                "tmdb_id": tmdb_id,
                "poster": format_tmdb_image(getattr(item, "poster_path", None)),
                "backdrop": format_tmdb_image(getattr(item, "backdrop_path", None), "original"),
                "subtitle": "TMDb",
            })
    except Exception as e:
        LOGGER.warning(f"TMDb movie candidate search failed for '{query}': {e}")

    return results[:limit]


async def search_tv_candidates(query: str, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        imdb_result = await search_title(query=query, type="tvSeries")
        if imdb_result and imdb_result.get("id"):
            key = ("imdb", imdb_result["id"])
            if key not in seen:
                seen.add(key)
                results.append({
                    "source": "imdb",
                    "title": imdb_result.get("title", ""),
                    "year": imdb_result.get("year", ""),
                    "imdb_id": imdb_result.get("id"),
                    "tmdb_id": imdb_result.get("moviedb_id"),
                    "poster": imdb_result.get("poster", ""),
                    "backdrop": "",
                    "subtitle": "IMDb / Cinemeta",
                })
    except Exception as e:
        LOGGER.warning(f"IMDb TV candidate search failed for '{query}': {e}")

    try:
        async with API_SEMAPHORE:
            tmdb_results = await tmdb.search().tv(query=query)

        for item in (tmdb_results or [])[:limit]:
            tmdb_id = getattr(item, "id", None)
            if not tmdb_id:
                continue

            imdb_id = None
            try:
                details = await _tmdb_tv_details(tmdb_id)
                ext = getattr(details, "external_ids", None) if details else None
                imdb_id = getattr(ext, "imdb_id", None) if ext else None
            except Exception:
                pass

            key = ("tmdb", str(tmdb_id))
            if key in seen:
                continue
            seen.add(key)

            first_air_date = getattr(item, "first_air_date", None)
            year_value = getattr(first_air_date, "year", None) if first_air_date else None

            results.append({
                "source": "tmdb",
                "title": getattr(item, "name", "") or "",
                "year": year_value or "",
                "imdb_id": imdb_id,
                "tmdb_id": tmdb_id,
                "poster": format_tmdb_image(getattr(item, "poster_path", None)),
                "backdrop": format_tmdb_image(getattr(item, "backdrop_path", None), "original"),
                "subtitle": "TMDb",
            })
    except Exception as e:
        LOGGER.warning(f"TMDb TV candidate search failed for '{query}': {e}")

    return results[:limit]


async def fetch_selected_movie_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    if not selected_id:
        return None

    data = await fetch_movie_metadata(
        title="manual-rescan",
        encoded_string=None,
        year=None,
        quality=None,
        default_id=selected_id
    )
    if not data:
        return None

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
        "media_type": "movie",
    }


async def fetch_selected_tv_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    if not selected_id:
        return None

    imdb_id = None
    tmdb_id = None
    imdb_tv = None
    use_tmdb = False

    if selected_id.startswith("tt"):
        imdb_id = selected_id
    elif selected_id.isdigit():
        tmdb_id = int(selected_id)
        use_tmdb = True
    else:
        return None

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

        tv = await _tmdb_tv_details(tmdb_id)
        if not tv:
            return None

        credits = getattr(tv, "credits", None) or {}
        cast_arr = getattr(credits, "cast", []) or []
        cast = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in cast_arr
        ]

        runtime_val = tv.episode_run_time[0] if getattr(tv, "episode_run_time", None) else None
        runtime = f"{runtime_val} min" if runtime_val else ""

        return {
            "tmdb_id": tv.id,
            "imdb_id": getattr(getattr(tv, "external_ids", None), "imdb_id", None),
            "title": tv.name,
            "release_year": getattr(tv.first_air_date, "year", 0) if getattr(tv, "first_air_date", None) else 0,
            "rating": getattr(tv, "vote_average", 0) or 0,
            "description": tv.overview or "",
            "poster": format_tmdb_image(tv.poster_path),
            "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
            "logo": get_tmdb_logo(getattr(tv, "images", None)),
            "genres": [g.name for g in (tv.genres or [])],
            "cast": cast,
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


# ----------------- Movie Metadata -----------------
async def fetch_movie_metadata(title, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id = None
    tmdb_id = None
    imdb_details = None
    use_tmdb = False

    # -------------------------------------------------------
    # 1. PROCESS DEFAULT ID (tt = IMDb, digits = TMDb)
    # -------------------------------------------------------
    if default_id:
        default_id = str(default_id).strip()

        if default_id.startswith("tt"):
            imdb_id = default_id
            use_tmdb = False                       
        elif default_id.isdigit():
            tmdb_id = int(default_id)
            use_tmdb = True                       

    # -------------------------------------------------------
    # 2. IF NO DEFAULT ID → SEARCH IMDb FIRST
    # -------------------------------------------------------
    if not imdb_id and not tmdb_id:
        imdb_id = await safe_imdb_search(
            f"{title} {year}" if year else title,
            "movie"
        )
        use_tmdb = not bool(imdb_id)

    # -------------------------------------------------------
    # 3. FETCH IMDb DETAILS (only if imdb_id exists)
    # -------------------------------------------------------
    if imdb_id and not use_tmdb:
        try:
            if imdb_id in IMDB_CACHE:
                imdb_details = IMDB_CACHE[imdb_id]
            else:
                async with API_SEMAPHORE:
                    imdb_details = await get_detail(
                        imdb_id=imdb_id,
                        media_type="movie"
                    )

                IMDB_CACHE[imdb_id] = imdb_details

        except Exception as e:
            LOGGER.warning(f"IMDb movie fetch failed [{title}] → {e}")
            imdb_details = None
            use_tmdb = True

    # -------------------------------------------------------
    # 4. DECIDE FINAL DATA SOURCE
    # -------------------------------------------------------
    must_use_tmdb = (
        use_tmdb or
        imdb_details is None or
        imdb_details == {}
    )

    # =======================================================
    #  5. TMDb MODE
    # =======================================================
    if must_use_tmdb:
        LOGGER.info(f"No valid IMDb data for '{title}' → using TMDb")
        tmdb_result = None

        # TMDb search if id unknown
        if not tmdb_id:
            tmdb_result = await safe_tmdb_search(title, "movie", year)
            if not tmdb_result:
                LOGGER.warning(f"No TMDb movie found for '{title}'")
                return None
            tmdb_id = tmdb_result.id

        # Fetch TMDb details
        movie = await _tmdb_movie_details(tmdb_id)
        if not movie:
            LOGGER.warning(f"TMDb details failed for {tmdb_id}")
            if tmdb_result:
                return _tmdb_movie_fallback(tmdb_result, title, encoded_string, year, quality)
            return {
                "tmdb_id": tmdb_id,
                "imdb_id": None,
                "title": title,
                "year": year or 0,
                "rate": 0,
                "description": "",
                "poster": "",
                "backdrop": "",
                "logo": "",
                "cast": [],
                "runtime": "",
                "media_type": "movie",
                "genres": [],
                "quality": quality,
                "encoded_string": encoded_string,
            }

        # Cast extraction
        credits = getattr(movie, "credits", None) or {}
        cast_arr = getattr(credits, "cast", []) or []
        cast_names = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in cast_arr
        ]

        runtime_val = getattr(movie, "runtime", None)
        runtime = f"{runtime_val} min" if runtime_val else ""

        return {
            "tmdb_id": movie.id,
            "imdb_id": getattr(movie.external_ids, "imdb_id", None),
            "title": movie.title,
            "year": getattr(movie.release_date, "year", 0) if getattr(movie, "release_date", None) else 0,
            "rate": getattr(movie, "vote_average", 0) or 0,
            "description": movie.overview or "",
            "poster": format_tmdb_image(movie.poster_path),
            "backdrop": format_tmdb_image(movie.backdrop_path, "original"),
            "logo": get_tmdb_logo(getattr(movie, "images", None)),
            "cast": cast_names,
            "runtime": str(runtime),
            "media_type": "movie",
            "genres": [g.name for g in (movie.genres or [])],
            "quality": quality,
            "encoded_string": encoded_string,
        }

    # =======================================================
    #  6. IMDb MODE
    # =======================================================
    images = format_imdb_images(imdb_id)
    imdb = imdb_details or {}

    return {
        "tmdb_id": imdb.get("moviedb_id") or imdb_id.replace("tt", ""),
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
