import asyncio
import traceback
import PTN
import re
from re import compile, IGNORECASE
from difflib import SequenceMatcher
from typing import Optional, List

from Backend.helper.imdb import get_detail, get_season, search_title, search_title_multi
from themoviedb import aioTMDb
from Backend.helper.settings_manager import SettingsManager
import Backend
from Backend.logger import LOGGER
from Backend.helper.encrypt import encode_string

# ----------------- Configuration -----------------

DELAY = 0
tmdb = aioTMDb(key=SettingsManager.current().tmdb_api, language="en-US", region="US")

# Cache dictionaries (per run)
IMDB_CACHE: dict = {}
TMDB_SEARCH_CACHE: dict = {}
TMDB_DETAILS_CACHE: dict = {}
EPISODE_CACHE: dict = {}

# Concurrency semaphore for external API calls
API_SEMAPHORE = asyncio.Semaphore(12)

# Minimum title-similarity score to accept a Cinemeta result
_CINEMETA_THRESHOLD = 0.60
# Minimum score to accept TMDb first-result (without year bonus it's stricter)
_TMDB_THRESHOLD = 0.55

# ----------------- Image helpers -----------------

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


# ----------------- ID extraction -----------------

def extract_default_id(text: str) -> str | None:
    """
    Extract an IMDb (tt…) or TMDb (numeric) ID from arbitrary text.

    Handles:
    • Bare IMDb IDs:      tt1234567
    • IMDb URLs:          https://www.imdb.com/title/tt1234567/
    • TMDb movie URLs:    https://www.themoviedb.org/movie/12345
    • TMDb TV URLs:       https://www.themoviedb.org/tv/12345
    • Bare TMDb IDs are intentionally NOT extracted from plain numbers to
      avoid false matches; they must come via a URL or an explicit override.
    """
    if not text:
        return None

    # 1. Bare IMDb ID  (tt followed by 7–10 digits, word-bounded)
    bare_imdb = re.search(r'\b(tt\d{7,10})\b', text)
    if bare_imdb:
        return bare_imdb.group(1)

    # 2. IMDb URL
    imdb_url = re.search(r'/title/(tt\d+)', text)
    if imdb_url:
        return imdb_url.group(1)

    # 3. TMDb URL  (/movie/NNN or /tv/NNN)
    tmdb_url = re.search(r'/(?:movie|tv)/(\d+)', text)
    if tmdb_url:
        return tmdb_url.group(1)

    return None


# ----------------- Fuzzy-matching helpers -----------------

def _normalize_title(title: str) -> str:
    """Lower-case, strip leading articles, collapse punctuation to spaces."""
    if not title:
        return ""
    t = title.lower().strip()
    # Remove leading articles
    t = re.sub(r'^\b(the|a|an)\b\s+', '', t)
    # Replace punctuation with spaces
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _title_similarity(t1: str, t2: str) -> float:
    """Fuzzy similarity between two titles, 0–1."""
    n1, n2 = _normalize_title(t1), _normalize_title(t2)
    if not n1 or not n2:
        return 0.0
    return SequenceMatcher(None, n1, n2).ratio()


def _year_from_str(year_val) -> int:
    """Extract the first 4-digit year from strings like '2023', '2020–', '2019-2022'."""
    if not year_val:
        return 0
    m = re.search(r'(\d{4})', str(year_val))
    return int(m.group(1)) if m else 0


def _score_candidate(
    query_title: str,
    query_year: Optional[int],
    result_title: str,
    result_year: int,
) -> float:
    """
    Combined score for a single API candidate.
    Base = title similarity; year match adds a bonus.
    """
    score = _title_similarity(query_title, result_title)

    if query_year and result_year:
        diff = abs(int(query_year) - result_year)
        if diff == 0:
            score = min(1.0, score + 0.20)   # exact year: strong boost
        elif diff == 1:
            score = min(1.0, score + 0.07)   # off by one: small boost
        elif diff <= 2:
            pass                              # neutral
        else:
            score = max(0.0, score - 0.10 * (diff - 2))  # growing penalty

    return score


def _build_query_variants(title: str, year: Optional[int] = None) -> List[str]:
    """
    Build an ordered list of search query variants to try when the primary
    query produces a poor match.

    Order: most-specific → least-specific.
    """
    variants: List[str] = []

    # 1. Original title (as parsed by PTN)
    variants.append(title)

    # 2. Title with year appended (often improves Cinemeta recall for movies)
    if year:
        variants.append(f"{title} {year}")

    # 3. Punctuation-stripped version (handles colons, hyphens, apostrophes)
    stripped = re.sub(r'[^\w\s]', ' ', title)
    stripped = re.sub(r'\s+', ' ', stripped).strip()
    if stripped and stripped.lower() != title.lower():
        variants.append(stripped)
        if year:
            variants.append(f"{stripped} {year}")

    # 4. Without leading article (The / A / An)
    no_article = re.sub(r'^\b(the|a|an)\b\s+', '', title, flags=IGNORECASE).strip()
    if no_article and no_article.lower() != title.lower():
        variants.append(no_article)

    # Deduplicate, preserve order, drop empty strings
    seen: set = set()
    result: List[str] = []
    for v in variants:
        k = v.lower()
        if k not in seen and v:
            seen.add(k)
            result.append(v)
    return result


# ----------------- Cached API wrappers -----------------

async def safe_imdb_search(title: str, type_: str, year: Optional[int] = None) -> str | None:
    """
    Search Cinemeta for *title* (+ optional *year*) with:
    • Multiple query variants (original, stripped, with-year, no-article)
    • Fuzzy title + year scoring across all returned results
    • Acceptance threshold so partial/wrong matches are rejected early
    • INFO log explaining what happened when no confident match is found
    """
    cache_key = f"imdb::{type_}::{title}::{year}"
    if cache_key in IMDB_CACHE:
        return IMDB_CACHE[cache_key]

    query_variants = _build_query_variants(title, year)

    best_id: str | None = None
    best_score: float = 0.0
    best_result_title: str = ""

    for query in query_variants:
        try:
            async with API_SEMAPHORE:
                results = await search_title_multi(query=query, type=type_, limit=8)

            for r in results:
                r_title = r.get("title", "")
                r_year = _year_from_str(r.get("year", ""))
                score = _score_candidate(title, year, r_title, r_year)

                if score > best_score:
                    best_score = score
                    best_id = r.get("id")
                    best_result_title = r_title

                # Early-exit: perfect / near-perfect match
                if best_score >= 0.92:
                    break

        except Exception as e:
            LOGGER.warning(f"Cinemeta search variant '{query}' [{type_}] failed: {e}")

        # Early-exit across variants too
        if best_score >= 0.92:
            break

    if best_score >= _CINEMETA_THRESHOLD and best_id:
        LOGGER.info(
            f"Cinemeta match: '{title}' (year={year}) → "
            f"'{best_result_title}' [{best_id}] (score={best_score:.2f})"
        )
        IMDB_CACHE[cache_key] = best_id
        return best_id

    # Log a clear, actionable INFO message so operators know why it fell back
    if best_id:
        LOGGER.info(
            f"Cinemeta low-confidence for '{title}' (year={year}, type={type_}) "
            f"| best candidate: '{best_result_title}' [{best_id}] score={best_score:.2f} "
            f"(threshold={_CINEMETA_THRESHOLD}) → falling back to TMDb"
        )
    else:
        LOGGER.info(
            f"Cinemeta returned no results for '{title}' (year={year}, type={type_}) "
            f"| tried variants: {query_variants} → falling back to TMDb"
        )

    IMDB_CACHE[cache_key] = None
    return None


async def safe_tmdb_search(title: str, type_: str, year: Optional[int] = None):
    """
    Search TMDb for *title*, scoring all returned results by title similarity
    and year proximity.  Falls back to a year-agnostic retry when the
    year-constrained search yields nothing.
    """
    cache_key = f"tmdb_search::{type_}::{title}::{year}"
    if cache_key in TMDB_SEARCH_CACHE:
        return TMDB_SEARCH_CACHE[cache_key]

    try:
        async with API_SEMAPHORE:
            if type_ == "movie":
                results = (
                    await tmdb.search().movies(query=title, year=year)
                    if year
                    else await tmdb.search().movies(query=title)
                )
                # Year-constrained search sometimes returns nothing for new/limited releases
                if not results and year:
                    async with API_SEMAPHORE:
                        results = await tmdb.search().movies(query=title)
            else:
                results = await tmdb.search().tv(query=title)

        best = _pick_best_tmdb_result(results, title, year, type_)

        if best is None and results:
            # Log what the top result actually was so operators can diagnose
            top = results[0]
            top_title = getattr(top, "title" if type_ == "movie" else "name", "?")
            LOGGER.info(
                f"TMDb '{title}' (year={year}) top result '{top_title}' "
                f"did not meet threshold – no confident match"
            )

        TMDB_SEARCH_CACHE[cache_key] = best
        return best

    except Exception as e:
        LOGGER.error(f"TMDb search failed for '{title}' [{type_}]: {e}")
        TMDB_SEARCH_CACHE[cache_key] = None
        return None


def _pick_best_tmdb_result(results, query_title: str, query_year: Optional[int], type_: str):
    """
    Score TMDb search results and return the best one above threshold,
    or None if nothing is confident enough.
    """
    if not results:
        return None

    best_item = None
    best_score = 0.0

    for item in results:
        if type_ == "movie":
            r_title = getattr(item, "title", "") or ""
            release = getattr(item, "release_date", None)
            r_year = getattr(release, "year", 0) if release else 0
        else:
            r_title = getattr(item, "name", "") or ""
            first_air = getattr(item, "first_air_date", None)
            r_year = getattr(first_air, "year", 0) if first_air else 0

        score = _score_candidate(query_title, query_year, r_title, r_year)

        if score > best_score:
            best_score = score
            best_item = item

        if best_score >= 0.92:
            break

    if best_score >= _TMDB_THRESHOLD and best_item is not None:
        return best_item

    return None


async def _tmdb_movie_details(movie_id):
    if movie_id in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[movie_id]
    try:
        async with API_SEMAPHORE:
            details = await tmdb.movie(movie_id).details(
                append_to_response="external_ids,credits"
            )
            images = await tmdb.movie(movie_id).images()
            details.images = images

        TMDB_DETAILS_CACHE[movie_id] = details
        return details
    except Exception as e:
        LOGGER.warning(f"TMDb movie details fetch failed for id={movie_id}: {e}")
        TMDB_DETAILS_CACHE[movie_id] = None
        return None


async def _tmdb_tv_details(tv_id):
    if tv_id in TMDB_DETAILS_CACHE:
        return TMDB_DETAILS_CACHE[tv_id]
    try:
        async with API_SEMAPHORE:
            details = await tmdb.tv(tv_id).details(
                append_to_response="external_ids,credits"
            )
            images = await tmdb.tv(tv_id).images()
            details.images = images
        TMDB_DETAILS_CACHE[tv_id] = details
        return details
    except Exception as e:
        LOGGER.warning(f"TMDb tv details fetch failed for id={tv_id}: {e}")
        TMDB_DETAILS_CACHE[tv_id] = None
        return None


async def _tmdb_episode_details(tv_id, season, episode):
    key = (tv_id, season, episode)
    if key in EPISODE_CACHE:
        return EPISODE_CACHE[key]
    try:
        async with API_SEMAPHORE:
            details = await tmdb.episode(tv_id, season, episode).details()
        EPISODE_CACHE[key] = details
        return details
    except Exception:
        EPISODE_CACHE[key] = None
        return None


# =============================================================================
# Main entry-point
# =============================================================================

async def metadata(filename: str, channel: int, msg_id, override_id: str = None) -> dict | None:
    try:
        parsed = PTN.parse(filename)
    except Exception as e:
        LOGGER.error(f"PTN parsing failed for {filename}: {e}\n{traceback.format_exc()}")
        return None

    # Skip combined/invalid files
    if "excess" in parsed and any("combined" in item.lower() for item in parsed["excess"]):
        LOGGER.info(f"Skipping {filename}: contains 'combined'")
        return None

    # Skip split/multipart files
    multipart_pattern = compile(r'(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)', IGNORECASE)
    if multipart_pattern.search(filename):
        LOGGER.info(f"Skipping {filename}: seems to be a split/multipart file")
        return None

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    year = parsed.get("year")
    quality = parsed.get("resolution")

    if isinstance(season, list) or isinstance(episode, list):
        LOGGER.warning(f"Invalid season/episode format for {filename}: {parsed}")
        return None
    if season and not episode:
        LOGGER.warning(f"Missing episode in {filename}: {parsed}")
        return None
    if not quality:
        LOGGER.warning(f"Skipping {filename}: No resolution (parsed={parsed})")
        return None
    if not title:
        LOGGER.info(f"No title parsed from: {filename} (parsed={parsed})")
        return None

    # --- Resolve override / default ID ---
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

    data = {"chat_id": channel, "msg_id": msg_id}
    try:
        encoded_string = await encode_string(data)
    except Exception:
        encoded_string = None

    try:
        if season and episode:
            LOGGER.info(f"Fetching TV metadata: {title} S{season:02d}E{episode:02d} (year={year})")
            return await fetch_tv_metadata(title, season, episode, encoded_string, year, quality, default_id)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} (year={year})")
            return await fetch_movie_metadata(title, encoded_string, year, quality, default_id)
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None


# =============================================================================
# TV metadata
# =============================================================================

async def fetch_tv_metadata(title, season, episode, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id = None
    tmdb_id = None
    imdb_tv = None
    imdb_ep = None
    use_tmdb = False

    # ------------------------------------------------------------------
    # 1. Handle explicit default ID
    # ------------------------------------------------------------------
    if default_id:
        default_id = str(default_id)
        if default_id.startswith("tt"):
            imdb_id = default_id
        elif default_id.isdigit():
            tmdb_id = int(default_id)
            use_tmdb = True

    # ------------------------------------------------------------------
    # 2. No ID → fuzzy Cinemeta search with query variants
    # ------------------------------------------------------------------
    if not imdb_id and not tmdb_id:
        imdb_id = await safe_imdb_search(title, "tvSeries", year)
        use_tmdb = not bool(imdb_id)

    # ------------------------------------------------------------------
    # 3. IMDb / Cinemeta detail fetch
    # ------------------------------------------------------------------
    if imdb_id and not use_tmdb:
        try:
            if imdb_id in IMDB_CACHE and isinstance(IMDB_CACHE.get(imdb_id), dict):
                imdb_tv = IMDB_CACHE[imdb_id]
            else:
                async with API_SEMAPHORE:
                    imdb_tv = await get_detail(imdb_id=imdb_id, media_type="tvSeries")
                IMDB_CACHE[imdb_id] = imdb_tv

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

    # ------------------------------------------------------------------
    # 4. Validate IMDb result against query title (guard against Cinemeta
    #    returning a stale wrong hit despite the fuzzy pre-filter)
    # ------------------------------------------------------------------
    if imdb_tv and not use_tmdb:
        matched_title = imdb_tv.get("title", "")
        sim = _title_similarity(title, matched_title)
        if sim < _CINEMETA_THRESHOLD:
            LOGGER.info(
                f"IMDb detail title mismatch for '{title}': "
                f"got '{matched_title}' (sim={sim:.2f}) → switching to TMDb"
            )
            imdb_tv = None
            use_tmdb = True

    # ------------------------------------------------------------------
    # 5. Fallback: TMDb
    # ------------------------------------------------------------------
    must_use_tmdb = use_tmdb or not imdb_tv

    if must_use_tmdb:
        LOGGER.info(f"No valid Cinemeta TV data for '{title}' S{season:02d}E{episode:02d} → using TMDb")

        if not tmdb_id:
            # Try with year first, then without
            tmdb_search = await safe_tmdb_search(title, "tv", year)
            if not tmdb_search and year:
                tmdb_search = await safe_tmdb_search(title, "tv", None)

            if not tmdb_search:
                LOGGER.info(
                    f"No TMDb TV result for '{title}' S{season:02d}E{episode:02d} "
                    f"(year={year}) – metadata unavailable"
                )
                return None
            tmdb_id = tmdb_search.id

        tv = await _tmdb_tv_details(tmdb_id)
        if not tv:
            LOGGER.info(f"TMDb TV details failed for id={tmdb_id} ('{title}')")
            return None

        ep = await _tmdb_episode_details(tmdb_id, season, episode)

        credits = getattr(tv, "credits", None) or {}
        cast_arr = getattr(credits, "cast", []) or []
        cast = [
            getattr(c, "name", None) or getattr(c, "original_name", None)
            for c in cast_arr
        ]

        ep_runtime = getattr(ep, "runtime", None) if ep else None
        series_runtime = tv.episode_run_time[0] if getattr(tv, "episode_run_time", None) else None
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
            "episode_title": getattr(ep, "name", f"S{season:02d}E{episode:02d}") if ep else f"S{season:02d}E{episode:02d}",
            "episode_backdrop": format_tmdb_image(getattr(ep, "still_path", None), "original") if ep else "",
            "episode_overview": getattr(ep, "overview", "") if ep else "",
            "episode_released": (
                ep.air_date.strftime("%Y-%m-%dT05:00:00.000Z")
                if getattr(ep, "air_date", None)
                else ""
            ),
            "quality": quality,
            "encoded_string": encoded_string,
        }

    # ------------------------------------------------------------------
    # 6. IMDb / Cinemeta path
    # ------------------------------------------------------------------
    imdb = imdb_tv or {}
    ep = imdb_ep or {}
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


# =============================================================================
# Movie metadata
# =============================================================================

async def fetch_movie_metadata(title, encoded_string, year=None, quality=None, default_id=None) -> dict | None:
    imdb_id = None
    tmdb_id = None
    imdb_details = None
    use_tmdb = False

    # ------------------------------------------------------------------
    # 1. Explicit default ID
    # ------------------------------------------------------------------
    if default_id:
        default_id = str(default_id).strip()
        if default_id.startswith("tt"):
            imdb_id = default_id
        elif default_id.isdigit():
            tmdb_id = int(default_id)
            use_tmdb = True

    # ------------------------------------------------------------------
    # 2. No ID → fuzzy Cinemeta search (tries year variants internally)
    # ------------------------------------------------------------------
    if not imdb_id and not tmdb_id:
        imdb_id = await safe_imdb_search(title, "movie", year)
        use_tmdb = not bool(imdb_id)

    # ------------------------------------------------------------------
    # 3. IMDb detail fetch
    # ------------------------------------------------------------------
    if imdb_id and not use_tmdb:
        try:
            if imdb_id in IMDB_CACHE and isinstance(IMDB_CACHE.get(imdb_id), dict):
                imdb_details = IMDB_CACHE[imdb_id]
            else:
                async with API_SEMAPHORE:
                    imdb_details = await get_detail(imdb_id=imdb_id, media_type="movie")
                IMDB_CACHE[imdb_id] = imdb_details

        except Exception as e:
            LOGGER.warning(f"IMDb movie fetch failed [{title}] → {e}")
            imdb_details = None
            use_tmdb = True

    # ------------------------------------------------------------------
    # 4. Validate IMDb result title against query
    # ------------------------------------------------------------------
    if imdb_details and not use_tmdb:
        matched_title = imdb_details.get("title", "")
        sim = _title_similarity(title, matched_title)
        if sim < _CINEMETA_THRESHOLD:
            LOGGER.info(
                f"IMDb detail title mismatch for '{title}': "
                f"got '{matched_title}' (sim={sim:.2f}) → switching to TMDb"
            )
            imdb_details = None
            use_tmdb = True

    # ------------------------------------------------------------------
    # 5. Fallback: TMDb
    # ------------------------------------------------------------------
    must_use_tmdb = use_tmdb or not imdb_details

    if must_use_tmdb:
        LOGGER.info(f"No valid Cinemeta movie data for '{title}' (year={year}) → using TMDb")

        if not tmdb_id:
            tmdb_result = await safe_tmdb_search(title, "movie", year)
            # Retry without year restriction for newer/unlisted titles
            if not tmdb_result and year:
                tmdb_result = await safe_tmdb_search(title, "movie", None)

            if not tmdb_result:
                LOGGER.info(
                    f"No TMDb movie found for '{title}' (year={year}) "
                    f"– metadata unavailable"
                )
                return None
            tmdb_id = tmdb_result.id

        movie = await _tmdb_movie_details(tmdb_id)
        if not movie:
            LOGGER.info(f"TMDb movie details failed for id={tmdb_id} ('{title}')")
            return None

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

    # ------------------------------------------------------------------
    # 6. IMDb / Cinemeta path
    # ------------------------------------------------------------------
    images = format_imdb_images(imdb_id)
    imdb = imdb_details or {}

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


# =============================================================================
# Candidate-search helpers (used by the /set command UI)
# =============================================================================

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
            tmdb_results = (
                await tmdb.search().movies(query=query, year=year)
                if year
                else await tmdb.search().movies(query=query)
            )

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


# =============================================================================
# Manual /set command helpers
# =============================================================================

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
