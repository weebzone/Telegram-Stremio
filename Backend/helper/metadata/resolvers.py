"""
resolvers.py — priority chains that pick the best metadata provider.

Anime  : Kitsu > TVDB > TMDB > Cinemeta
Movies : TMDB > Cinemeta
Series : TVDB > Cinemeta > TMDB

Each resolve_* function tries providers in order and returns the first
good match (or None).

Example
-------
    from Backend.helper.metadata.resolvers import resolve_movie, resolve_tv

    movie = await resolve_movie("Inception", encoded_string, year=2010)
    series = await resolve_tv("Breaking Bad", encoded_string, year=2008)
"""

from __future__ import annotations

from typing import Optional

from Backend.helper.metadata.common import split_default_id, title_similarity, CINEMETA_THRESHOLD
from Backend.helper.metadata.providers import cinemeta, kitsu, tmdb, tvdb
from Backend.logger import LOGGER


async def resolve_movie(
    title: str,
    encoded_string,
    year=None,
    quality=None,
    default_id=None,
) -> Optional[dict]:
    imdb_id, tmdb_id, explicit_imdb, force_tmdb = split_default_id(default_id)

    if tmdb_id and force_tmdb:
        movie = await tmdb.details("movie", tmdb_id)
        if movie:
            return tmdb.build_movie_payload(movie, quality, encoded_string)

    if imdb_id and explicit_imdb:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "movie")
            if detail:
                return cinemeta.build_movie_payload(detail, imdb_id, title, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"Cinemeta explicit movie fetch failed [{imdb_id}]: {e}")

    if not tmdb_id:
        hit = await tmdb.safe_search(title, "movie", year)
        if hit:
            tmdb_id = hit.id
    if tmdb_id:
        movie = await tmdb.details("movie", tmdb_id)
        if movie:
            LOGGER.info(f"[MOVIE] TMDB hit for '{title}' (year={year})")
            return tmdb.build_movie_payload(movie, quality, encoded_string)

    LOGGER.info(f"[MOVIE] TMDB miss for '{title}' -> Cinemeta")
    if not imdb_id:
        imdb_id = await cinemeta.safe_search(title, "movie", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "movie")
            if detail:
                sim = title_similarity(title, detail.get("title", ""))
                if sim >= CINEMETA_THRESHOLD or explicit_imdb:
                    return cinemeta.build_movie_payload(
                        detail, imdb_id, title, quality, encoded_string
                    )
                LOGGER.info(
                    f"[MOVIE] Cinemeta title mismatch for '{title}': "
                    f"got '{detail.get('title')}' (sim={sim:.2f})"
                )
        except Exception as e:
            LOGGER.warning(f"Cinemeta movie fetch failed [{title}]: {e}")

    LOGGER.info(f"[MOVIE] No metadata for '{title}' (year={year})")
    return None


async def resolve_series(
    title: str,
    season: int,
    episode: int,
    encoded_string,
    year=None,
    quality=None,
    default_id=None,
) -> Optional[dict]:
    imdb_id, tmdb_id, explicit_imdb, force_tmdb = split_default_id(default_id)

    if tmdb_id and force_tmdb:
        tv = await tmdb.details("tv", tmdb_id)
        if tv:
            ep = await tmdb.episode_details(tmdb_id, season, episode)
            return tmdb.build_tv_payload(tv, ep, season, episode, quality, encoded_string)

    if imdb_id and explicit_imdb:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "tvSeries")
            ep = await cinemeta.cached_season(imdb_id, season, episode)
            if detail:
                return cinemeta.build_tv_payload(
                    detail, ep or {}, imdb_id, title, season, episode, quality, encoded_string
                )
        except Exception as e:
            LOGGER.warning(f"Cinemeta explicit TV fetch failed [{imdb_id}]: {e}")

    try:
        result = await tvdb.fetch_series_metadata(
            title, season, episode, encoded_string, year=year, quality=quality
        )
        if result:
            LOGGER.info(f"[SERIES] TVDB hit for '{title}' S{season:02d}E{episode:02d}")
            return result
    except Exception as e:
        LOGGER.warning(f"[SERIES] TVDB error for '{title}': {e}")

    LOGGER.info(f"[SERIES] TVDB miss for '{title}' -> Cinemeta")
    if not imdb_id:
        imdb_id = await cinemeta.safe_search(title, "tvSeries", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "tvSeries")
            ep = await cinemeta.cached_season(imdb_id, season, episode)
            if detail:
                sim = title_similarity(title, detail.get("title", ""))
                if sim >= CINEMETA_THRESHOLD or explicit_imdb:
                    return cinemeta.build_tv_payload(
                        detail, ep or {}, imdb_id, title, season, episode, quality, encoded_string
                    )
                LOGGER.info(
                    f"[SERIES] Cinemeta title mismatch for '{title}': "
                    f"got '{detail.get('title')}' (sim={sim:.2f})"
                )
        except Exception as e:
            LOGGER.warning(f"Cinemeta TV fetch failed [{title}]: {e}")

    LOGGER.info(f"[SERIES] Cinemeta miss for '{title}' -> TMDB")
    if not tmdb_id:
        hit = await tmdb.safe_search(title, "tv", year)
        if hit:
            tmdb_id = hit.id
    if tmdb_id:
        tv = await tmdb.details("tv", tmdb_id)
        if tv:
            ep = await tmdb.episode_details(tmdb_id, season, episode)
            return tmdb.build_tv_payload(tv, ep, season, episode, quality, encoded_string)

    LOGGER.info(f"[SERIES] No metadata for '{title}' S{season:02d}E{episode:02d}")
    return None


async def resolve_anime_tv(
    title: str,
    season,
    episode: int,
    encoded_string,
    year=None,
    quality=None,
    absolute: bool = False,
) -> Optional[dict]:
    """Resolve anime episode metadata.

    absolute=True (or season is None): orphan/absolute numbering
    e.g. "One Piece 1223 720.mkv" → Kitsu absolute episode 1223.
    """
    absolute = bool(absolute or season is None)
    label = f"E{episode}" if absolute else f"S{int(season):02d}E{int(episode):02d}"

    try:
        result = await kitsu.fetch_anime_tv(
            title,
            season,
            episode,
            encoded_string,
            year=year,
            quality=quality,
            absolute=absolute,
        )
        if result:
            LOGGER.info(f"[ANIME] Kitsu hit for '{title}' {label}")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] Kitsu error for '{title}': {e}")

    use_season = 1 if absolute else int(season)
    use_episode = int(episode)

    try:
        result = await tvdb.fetch_series_metadata(
            title, use_season, use_episode, encoded_string, year=year, quality=quality
        )
        if result:
            if absolute:
                result["season_number"] = result.get("season_number") or use_season
                result["episode_number"] = use_episode
                result["absolute_episode"] = use_episode
            LOGGER.info(f"[ANIME] TVDB hit for '{title}' {label}")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] TVDB error for '{title}': {e}")

    hit = await tmdb.safe_search(title, "tv", year)
    if hit:
        tv = await tmdb.details("tv", hit.id)
        if tv:
            ep = None if absolute else await tmdb.episode_details(hit.id, use_season, use_episode)
            LOGGER.info(f"[ANIME] TMDB hit for '{title}' {label}")
            payload = tmdb.build_tv_payload(
                tv, ep, use_season, use_episode, quality, encoded_string
            )
            if absolute:
                payload["absolute_episode"] = use_episode
                if not payload.get("episode_title") or payload["episode_title"].startswith("S"):
                    payload["episode_title"] = f"Episode {use_episode}"
            return payload

    imdb_id = await cinemeta.safe_search(title, "tvSeries", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "tvSeries")
            ep = {} if absolute else await cinemeta.cached_season(imdb_id, use_season, use_episode)
            if detail:
                LOGGER.info(f"[ANIME] Cinemeta hit for '{title}' {label}")
                payload = cinemeta.build_tv_payload(
                    detail,
                    ep or {},
                    imdb_id,
                    title,
                    use_season,
                    use_episode,
                    quality,
                    encoded_string,
                )
                if absolute:
                    payload["absolute_episode"] = use_episode
                    if not (ep or {}).get("title"):
                        payload["episode_title"] = f"Episode {use_episode}"
                return payload
        except Exception as e:
            LOGGER.warning(f"[ANIME] Cinemeta error for '{title}': {e}")

    LOGGER.info(f"[ANIME] No metadata for '{title}' {label}")
    return None


async def resolve_anime_movie(
    title: str,
    encoded_string,
    year=None,
    quality=None,
) -> Optional[dict]:
    try:
        result = await kitsu.fetch_anime_movie(title, encoded_string, year=year, quality=quality)
        if result:
            LOGGER.info(f"[ANIME] Kitsu movie hit for '{title}'")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] Kitsu movie error for '{title}': {e}")

    try:
        result = await tvdb.fetch_movie_metadata(title, encoded_string, year=year, quality=quality)
        if result:
            LOGGER.info(f"[ANIME] TVDB movie hit for '{title}'")
            return result
    except Exception as e:
        LOGGER.warning(f"[ANIME] TVDB movie error for '{title}': {e}")

    hit = await tmdb.safe_search(title, "movie", year)
    if hit:
        movie = await tmdb.details("movie", hit.id)
        if movie:
            LOGGER.info(f"[ANIME] TMDB movie hit for '{title}'")
            return tmdb.build_movie_payload(movie, quality, encoded_string)

    imdb_id = await cinemeta.safe_search(title, "movie", year)
    if imdb_id:
        try:
            detail = await cinemeta.cached_detail(imdb_id, "movie")
            if detail:
                LOGGER.info(f"[ANIME] Cinemeta movie hit for '{title}'")
                return cinemeta.build_movie_payload(detail, imdb_id, title, quality, encoded_string)
        except Exception as e:
            LOGGER.warning(f"[ANIME] Cinemeta movie error for '{title}': {e}")

    LOGGER.info(f"[ANIME] No movie metadata for '{title}'")
    return None
