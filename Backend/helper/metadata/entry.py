"""Main metadata entry point and /set candidate search."""
from __future__ import annotations

import traceback
from typing import Optional

import Backend
from Backend.helper.encrypt import encode_string
from Backend.helper.metadata.common import (
    ensure_media_ids,
    COMBINED_EPISODE_BASE,
    COMBINED_SEASON,
    extract_default_id,
    format_imdb_images,
    format_tmdb_image,
    resolve_cover_url,
    split_default_id,
)
from Backend.helper.metadata.parse import (
    analyze_metadata_failure,
    apply_combined_override,
    clean_anime_search_title,
    extract_absolute_episode,
    is_absolute_episode,
    is_multipart_video,
    parse_media_name,
)
from Backend.helper.metadata.providers import cinemeta, tmdb
from Backend.helper.metadata.resolvers import (
    resolve_anime_movie,
    resolve_anime_tv,
    resolve_movie,
    resolve_series,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix
from Backend.logger import LOGGER


def _is_anime_channel(channel) -> bool:
    anime_channels = SettingsManager.current().anime_channels
    if not anime_channels:
        return False
    target = str(channel).replace("-100", "")
    return any(str(c).strip().replace("-100", "") == target for c in anime_channels)


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


async def metadata(
    filename: str,
    channel: int,
    msg_id,
    override_id: str = None,
    season_hint: int = None,
) -> dict | None:
    if is_multipart_video(filename):
        LOGGER.info(f"Skipping {filename}: split video file not meant to be combined in Stremio")
        return None

    split_info = parse_split_info(filename)
    part_number = split_info[1] if split_info else None
    parse_target = strip_part_suffix(filename) if split_info else filename

    try:
        parsed = parse_media_name(parse_target)
    except Exception as e:
        LOGGER.error(f"Parsing failed for {filename}: {e}\n{traceback.format_exc()}")
        return None

    combined = parse_combined_episodes(parse_target)

    excess = parsed.get("excess")
    if not combined and excess and any("combined" in item.lower() for item in excess):
        LOGGER.info(f"Skipping {filename}: contains 'combined'")
        return None

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    year = parsed.get("year")
    quality = parsed.get("quality")

    if season_hint is not None and episode and not season and not isinstance(episode, list):
        season = season_hint

    # GuessIt sometimes yields season=0 for absolute anime releases — treat as no season
    try:
        if season is not None and int(season) == 0:
            season = None
    except (TypeError, ValueError):
        pass

    if combined:
        season, episode = combined["season"], combined["start"] or 1
    elif isinstance(season, list) or isinstance(episode, list):
        LOGGER.warning(f"Invalid season/episode format for {filename}: {parsed}")
        return None
    elif season and not episode:
        combined = {"season": season, "start": None, "end": None}
        episode = 1

    # Absolute / orphan episode (e.g. "One Piece 1223 720.mkv",
    # "Naruto Shippuden - 016 480p ...", "[Judas] One Piece - 1172.mkv")
    absolute = False
    if episode is None and not season:
        abs_ep = extract_absolute_episode(filename, parsed)
        if abs_ep is not None:
            episode = abs_ep
            absolute = True
            parsed["episode"] = abs_ep
    elif is_absolute_episode(parsed, filename):
        absolute = True
        if episode is None:
            episode = extract_absolute_episode(filename, parsed)

    # On anime channels, recover absolute from the raw filename when
    # PTN/GuessIt treated a numbered release as a movie (no season/episode).
    anime_channel_early = _is_anime_channel(channel)
    if (
        anime_channel_early
        and not season
        and not absolute
        and episode is None
    ):
        abs_ep = extract_absolute_episode(filename, parsed)
        if abs_ep is not None:
            episode = abs_ep
            absolute = True
            parsed["episode"] = abs_ep

    if not quality:
        LOGGER.warning(f"Skipping {filename}: No resolution (parsed={parsed})")
        return None
    if not title:
        LOGGER.info(f"No title parsed from: {filename} (parsed={parsed})")
        return None

    # Strip absolute episode / release-group noise from the search title so
    # "One Piece - 1172" does not match "One Piece Egghead Arc Recap".
    if absolute and episode is not None:
        title = clean_anime_search_title(title, int(episode))
    else:
        title = clean_anime_search_title(title, None)

    default_id = _resolve_default_id(override_id, filename)

    try:
        encoded_string = await encode_string({"chat_id": channel, "msg_id": msg_id})
    except Exception:
        encoded_string = None

    group_key = f"{channel}:{quality}:{split_info[0]}" if split_info else None
    # Single (non-split) .zip archives
    if group_key is None and filename and filename.lower().rstrip().endswith(".zip"):
        from Backend.helper.split_files import _normalize

        base = filename.rsplit(".", 1)[0]
        group_key = f"{channel}:{quality}:{_normalize(base)}.zip"
        part_number = 1

    anime_channel = _is_anime_channel(channel)

    try:
        # TV path: classic SxxExx, or absolute/orphan episode on anime channels
        is_tv = bool(season and episode) or (absolute and episode and anime_channel)
        if is_tv:
            if absolute:
                LOGGER.info(f"Fetching TV metadata (absolute): {title} E{int(episode)} (year={year})")
            else:
                LOGGER.info(f"Fetching TV metadata: {title} S{int(season):02d}E{int(episode):02d} (year={year})")
            result = None
            if not default_id and anime_channel:
                result = await resolve_anime_tv(
                    title, season, int(episode), encoded_string,
                    year=year, quality=quality, absolute=absolute,
                )
            if result is None and not absolute:
                result = await resolve_series(
                    title, int(season), int(episode), encoded_string,
                    year=year, quality=quality, default_id=default_id,
                )
            # Absolute on non-anime channel: still try series with season 1
            if result is None and absolute:
                result = await resolve_series(
                    title, 1, int(episode), encoded_string,
                    year=year, quality=quality, default_id=default_id,
                )
                if result:
                    result["absolute_episode"] = int(episode)
                    result["episode_number"] = int(episode)
            if result is not None and combined:
                apply_combined_override(result, combined)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} (year={year})")
            result = None
            if not default_id and anime_channel:
                result = await resolve_anime_movie(
                    title, encoded_string, year=year, quality=quality
                )
            if result is None:
                result = await resolve_movie(
                    title, encoded_string, year=year, quality=quality, default_id=default_id
                )
        if result is not None:
            if anime_channel:
                result["is_anime"] = True
            result["group_key"] = group_key
            result["part_number"] = part_number
        if result:
            ensure_media_ids(result, seed=str(result.get("title") or ""))
        return result
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None


# ── /set candidate search ─────────────────────────────────────────────────────

def _candidate_entry(source, title, year, imdb_id, tmdb_id, poster, backdrop, subtitle, media_type=None) -> dict:
    selected_id = imdb_id if (source == "imdb" and imdb_id) else (
        str(tmdb_id) if tmdb_id else (imdb_id or None)
    )
    return {
        "source": source,
        "media_type": media_type,
        "title": title or "",
        "year": year or "",
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "selected_id": selected_id,
        "poster": poster,
        "backdrop": backdrop,
        "subtitle": subtitle,
    }


async def _resolve_id_candidate(default_id, media_type: str) -> dict | None:
    imdb_id, tmdb_id, _explicit_imdb, use_tmdb = split_default_id(default_id)

    if imdb_id and not use_tmdb:
        imdb_type = "movie" if media_type == "movie" else "tvSeries"
        detail = None
        try:
            detail = await cinemeta.cached_detail(imdb_id, imdb_type)
        except Exception as e:
            LOGGER.warning(f"IMDb id candidate resolve failed for '{imdb_id}': {e}")
        images = format_imdb_images(imdb_id)
        if detail and detail.get("title"):
            return _candidate_entry(
                "imdb", detail.get("title", ""), detail.get("releaseDetailed", {}).get("year", ""),
                imdb_id, detail.get("moviedb_id"), detail.get("poster") or images["poster"],
                detail.get("background") or images["backdrop"], "IMDb / Cinemeta", media_type,
            )
        return _candidate_entry(
            "imdb", "", "", imdb_id, None, images["poster"], images["backdrop"],
            "IMDb / Cinemeta", media_type,
        )

    if tmdb_id:
        details = await tmdb.details(media_type, tmdb_id)
        if not details:
            return None
        r_title, r_year = tmdb.tmdb_title_year(details, media_type)
        imdb_ext = getattr(getattr(details, "external_ids", None), "imdb_id", None)
        return _candidate_entry(
            "tmdb", r_title, r_year or "", imdb_ext, tmdb_id,
            format_tmdb_image(getattr(details, "poster_path", None)),
            format_tmdb_image(getattr(details, "backdrop_path", None), "original"),
            "TMDb", media_type,
        )
    return None


async def _search_candidates(query: str, media_type: str, year: int | None = None, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    default_id = extract_default_id(query)
    if default_id:
        candidate = await _resolve_id_candidate(default_id, media_type)
        return [candidate] if candidate else []

    imdb_type = "movie" if media_type == "movie" else "tvSeries"
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        imdb_hits = await cinemeta.search_title_multi(query=query, type=imdb_type, limit=limit)
        for hit in imdb_hits:
            hid = hit.get("id")
            if not hid or ("imdb", hid) in seen:
                continue
            seen.add(("imdb", hid))
            images = format_imdb_images(hid)
            results.append(_candidate_entry(
                "imdb", hit.get("title", ""), hit.get("year", ""),
                hid, None, hit.get("poster") or images["poster"], images["backdrop"],
                "IMDb / Cinemeta", media_type,
            ))
    except Exception as e:
        LOGGER.warning(f"IMDb {media_type} candidate search failed for '{query}': {e}")

    try:
        tmdb_results = await tmdb.raw_search(query, media_type, year if media_type == "movie" else None)
        for item in (tmdb_results or [])[:limit]:
            tid = getattr(item, "id", None)
            if not tid or ("tmdb", str(tid)) in seen:
                continue
            seen.add(("tmdb", str(tid)))
            imdb_id = await tmdb.external_imdb_id(media_type, tid)
            r_title, r_year = tmdb.tmdb_title_year(item, media_type)
            results.append(_candidate_entry(
                "tmdb", r_title, r_year or "", imdb_id, tid,
                format_tmdb_image(getattr(item, "poster_path", None)),
                format_tmdb_image(getattr(item, "backdrop_path", None), "original"),
                "TMDb", media_type,
            ))
    except Exception as e:
        LOGGER.warning(f"TMDb {media_type} candidate search failed for '{query}': {e}")

    return results[:limit]


async def search_movie_candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    return await _search_candidates(query, "movie", year, limit)


async def search_tv_candidates(query: str, limit: int = 8) -> list[dict]:
    return await _search_candidates(query, "tv", None, limit)


async def search_any_candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    default_id = extract_default_id(query)
    if default_id:
        out: list[dict] = []
        seen: set[tuple] = set()
        for mt in ("movie", "tv"):
            candidate = await _resolve_id_candidate(default_id, mt)
            if not candidate or not candidate.get("title"):
                continue
            key = (candidate.get("imdb_id"), str(candidate.get("tmdb_id")), mt)
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out
    results = await search_movie_candidates(query, year, limit)
    results += await search_tv_candidates(query, limit)
    return results


def build_id_link(imdb_id=None, tmdb_id=None, media_type: str = "movie") -> str | None:
    if imdb_id and str(imdb_id).startswith("tt"):
        return f"https://www.imdb.com/title/{imdb_id}/"
    if tmdb_id is not None and str(tmdb_id).lstrip("-").isdigit() and int(tmdb_id) > 0:
        path = "movie" if media_type == "movie" else "tv"
        return f"https://www.themoviedb.org/{path}/{tmdb_id}"
    return None


def caption_with_id(caption: str, metadata_info: dict) -> str | None:
    link = build_id_link(
        metadata_info.get("imdb_id"), metadata_info.get("tmdb_id"),
        metadata_info.get("media_type", "movie"),
    )
    if not link:
        return None
    base = (caption or "").strip()
    if extract_default_id(base):
        return None
    return f"{base}\n{link}" if base else link


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


async def fetch_selected_movie_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    if not selected_id:
        return None
    data = await resolve_movie(
        title="manual-rescan", encoded_string=None, year=None, quality=None, default_id=selected_id
    )
    return _to_selection_payload(data, "movie") if data else None


async def fetch_selected_tv_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    imdb_id, tmdb_id, _, use_tmdb = split_default_id(selected_id)
    if not imdb_id and not tmdb_id:
        return None

    imdb_tv = None
    if imdb_id and not use_tmdb:
        try:
            imdb_tv = await cinemeta.get_detail(imdb_id=imdb_id, media_type="tvSeries")
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
        tv = await tmdb.details("tv", tmdb_id)
        if not tv:
            return None
        first_air = getattr(tv, "first_air_date", None)
        runtime = ""
        if getattr(tv, "episode_run_time", None):
            runtime = f"{tv.episode_run_time[0]} min"
        return {
            "tmdb_id": tv.id,
            "imdb_id": getattr(getattr(tv, "external_ids", None), "imdb_id", None),
            "title": tv.name,
            "release_year": getattr(first_air, "year", 0) if first_air else 0,
            "rating": getattr(tv, "vote_average", 0) or 0,
            "description": tv.overview or "",
            "poster": format_tmdb_image(tv.poster_path),
            "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
            "logo": tmdb.get_tmdb_logo(getattr(tv, "images", None)),
            "genres": [g.name for g in (tv.genres or [])],
            "cast": [
                getattr(c, "name", None) or getattr(c, "original_name", None)
                for c in (getattr(getattr(tv, "credits", None), "cast", None) or [])
            ],
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
