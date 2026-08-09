"""Kitsu anime metadata provider (with ani.zip mappings for IMDb/TMDb/episode art)."""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

import httpx
from rapidfuzz import fuzz

from Backend.helper.metadata.common import (
    ensure_media_ids,
    KITSU_CACHE,
    KITSU_THRESHOLD,
    cached_call,
    logo_from_imdb,
    normalize_rating,
    parse_year_range,
    strip_html,
)
from Backend.logger import LOGGER

KITSU_URL = "https://kitsu.io/api/edge"
ANIZIP_URL = "https://api.ani.zip/mappings"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "User-Agent": "Telegram-Stremio (+https://github.com/weebzone/Telegram-Stremio)",
}


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)
        return _client


def _normalize(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        return max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0
    except Exception:
        return 0.0


def _title_score(query: str, attrs: dict) -> float:
    """Score against every Kitsu title variant + abbreviations (alias-aware)."""
    from Backend.helper.metadata.common import score_candidate_aliases
    titles = attrs.get("titles") or {}
    primary = (
        attrs.get("canonicalTitle")
        or titles.get("en")
        or titles.get("en_jp")
        or titles.get("ja_jp")
        or ""
    )
    aliases = []
    aliases.extend(titles.values() if isinstance(titles, dict) else [])
    aliases.extend(attrs.get("abbreviatedTitles") or [])
    # slug as last-resort alias (e.g. one-piece -> one piece)
    slug = attrs.get("slug")
    if slug:
        aliases.append(str(slug).replace("-", " "))
    return score_candidate_aliases(
        query, None, primary, 0,
        aliases=aliases,
        year_reliable=False,
        year_lower_bound=True,
    )


def _season_queries(title: str, season: Optional[int]) -> List[str]:
    if season and int(season) > 1:
        return [f"{title} Season {season}", f"{title} {season}", title]
    return [title]


async def _kitsu_search(query: str, subtype: Optional[str] = None) -> Optional[dict]:
    try:
        client = await _get_client()
        params = {"filter[text]": query, "page[limit]": 8}
        if subtype:
            params["filter[subtype]"] = subtype
        resp = await client.get(f"{KITSU_URL}/anime", params=params)
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or []
        return data
    except Exception as e:
        LOGGER.warning(f"[KITSU] search failed for '{query}': {e}")
        return None


async def search_anime(title: str, season: Optional[int] = None, movie: bool = False) -> Optional[dict]:
    cache_key = f"kitsu::{'movie' if movie else 'tv'}::{title}::{season}"

    async def _produce():
        best = None
        best_score = 0.0
        subtype = "movie" if movie else None
        for query in _season_queries(title, None if movie else season):
            rows = await _kitsu_search(query, subtype=subtype) or []
            for row in rows:
                attrs = row.get("attributes") or {}
                # Prefer TV for series searches
                if not movie and attrs.get("subtype") in ("movie", "music"):
                    continue
                if movie and attrs.get("subtype") not in ("movie", None, "special", "OVA", "ONA"):
                    # still allow movie subtype primarily
                    if attrs.get("subtype") not in ("movie",):
                        continue
                score = _title_score(title, attrs)
                if score > best_score:
                    best_score = score
                    best = row
            if best_score >= 0.92:
                break

        if best and best_score >= KITSU_THRESHOLD:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] match '{title}' -> '{attrs.get('canonicalTitle')}' "
                f"[{best.get('id')}] score={best_score:.2f}"
            )
            return best
        if best:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] low-confidence for '{title}': "
                f"'{attrs.get('canonicalTitle')}' score={best_score:.2f}"
            )
        return None

    return await cached_call(KITSU_CACHE, cache_key, "kitsu_search", _produce)


async def get_anizip_mappings(kitsu_id: int) -> Optional[dict]:
    cache_key = f"anizip::{kitsu_id}"

    async def _produce():
        try:
            client = await _get_client()
            resp = await client.get(ANIZIP_URL, params={"kitsu_id": kitsu_id})
            return resp.json() if resp.status_code == 200 else None
        except Exception as e:
            LOGGER.warning(f"[KITSU] ani.zip mappings failed for {kitsu_id}: {e}")
            return None

    return await cached_call(KITSU_CACHE, cache_key, "anizip", _produce)


def _anizip_image(images, cover_type: str) -> str:
    for img in images or []:
        if str(img.get("coverType", "")).lower() == cover_type.lower() and img.get("url"):
            return img["url"]
    return ""


def _poster(attrs: dict, images: list) -> str:
    poster = attrs.get("posterImage") or {}
    return (
        poster.get("original")
        or poster.get("large")
        or poster.get("medium")
        or _anizip_image(images, "Poster")
        or ""
    )


def _backdrop(attrs: dict, images: list) -> str:
    cover = attrs.get("coverImage") or {}
    return (
        cover.get("original")
        or cover.get("large")
        or _anizip_image(images, "Fanart")
        or _anizip_image(images, "Banner")
        or ""
    )


def _common_payload(row: dict, doc: dict, title: str) -> dict:
    attrs = row.get("attributes") or {}
    mappings = (doc or {}).get("mappings") or {}
    tmdb_id = mappings.get("themoviedb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (ValueError, TypeError):
        tmdb_id = None

    titles = attrs.get("titles") or {}
    images = (doc or {}).get("images") or []
    rate = normalize_rating(
        (float(attrs["averageRating"]) / 10.0) if attrs.get("averageRating") else 0
    )
    year, year_end = parse_year_range(attrs.get("startDate"), attrs.get("endDate"))
    duration = attrs.get("episodeLength")
    imdb_id = mappings.get("imdb_id")
    # Prefer English display title; keep original/romaji separately for search
    english = titles.get("en") or titles.get("en_us") or titles.get("en_jp") or ""
    original = (
        attrs.get("canonicalTitle")
        or titles.get("ja_jp")
        or titles.get("en_jp")
        or title
    )
    display = english or original or title
    logo = _anizip_image(images, "Clearlogo") or logo_from_imdb(imdb_id)
    payload = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": display,
        "title_english": english or display,
        "original_title": original if original != display else "",
        "year": year,
        "year_end": year_end,
        "rate": rate,
        "description": strip_html(_pick_english_text(attrs.get("synopsis"), attrs.get("description")) or ""),
        "poster": _poster(attrs, images),
        "backdrop": _backdrop(attrs, images),
        "logo": logo,
        "genres": [],
        "cast": [],
        "runtime": f"{duration} min" if duration else "",
        "kitsu_id": int(row["id"]) if row.get("id") is not None else None,
    }
    return ensure_media_ids(payload, seed=f"kitsu:{row.get('id')}")


def _pick_english_text(*candidates, allow_fallback: bool = True) -> str:
    """Prefer English / latin-script text. Optionally fall back to any language."""
    import re
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
    latin = re.compile(r"[A-Za-z]")
    best_any = ""
    for raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, dict):
            for key in ("en", "en_us", "en_jp", "x-jat", "romaji"):
                val = raw.get(key)
                if val and str(val).strip():
                    return str(val).strip()
            for val in raw.values():
                s = str(val or "").strip()
                if not s:
                    continue
                if latin.search(s):
                    return s
                if not best_any:
                    best_any = s
            continue
        s = str(raw).strip()
        if not s:
            continue
        if latin.search(s):
            return s
        if not best_any:
            best_any = s
    return best_any if allow_fallback else ""


def _is_mostly_cjk(text: str) -> bool:
    if not text:
        return False
    import re
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
    latin = re.compile(r"[A-Za-z]")
    s = str(text)
    return bool(cjk.search(s)) and not bool(latin.search(s))


def _needs_english(text: str) -> bool:
    """True when field is empty, generic, or non-English (CJK-only)."""
    if not text or not str(text).strip():
        return True
    s = str(text).strip()
    if s.startswith(("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")) and "E" in s[:6]:
        return True
    if s.startswith("Episode "):
        return True
    return _is_mostly_cjk(s)


def _episode_title_fallback(season: int, episode: int, absolute: bool = False) -> str:
    if absolute or season is None:
        return f"Episode {episode}"
    return f"S{int(season):02d}E{int(episode):02d}"


def _anizip_episode_fields(ep: dict) -> dict:
    """Extract preferred English fields from a single ani.zip episode object."""
    if not ep:
        return {}
    title = _pick_english_text(ep.get("title"), allow_fallback=False)
    overview = _pick_english_text(
        ep.get("overview"), ep.get("summary"), allow_fallback=False
    )
    image = (ep.get("image") or "").strip()
    rating = None
    raw_r = ep.get("rating")
    if raw_r is not None and str(raw_r).strip() != "":
        try:
            rating = float(raw_r)
        except (TypeError, ValueError):
            rating = None
    return {
        "episode_title": title or "",
        "episode_overview": overview or "",
        "episode_backdrop": image,
        "episode_released": ep.get("airDate") or ep.get("airdate") or "",
        "episode_rating": rating,
    }


async def _tvdb_episode_fields(tvdb_id: int, season: int, episode: int, absolute_hint=None) -> dict:
    """Cached TVDB episode fields (English). Empty dict if unavailable."""
    try:
        from Backend.helper.metadata.providers import tvdb as tvdb_mod
        if not tvdb_mod.tvdb_api_key():
            return {}
        ep = None
        if absolute_hint and (season is None or int(season) < 1):
            ep = await tvdb_mod.episode_by_absolute(int(tvdb_id), int(absolute_hint))
        if ep is None:
            ep = await tvdb_mod.episode_by_number(int(tvdb_id), int(season or 1), int(episode))
        if not ep:
            return {}
        name = (ep.get("name") or "").strip()
        overview = (ep.get("overview") or "").strip()
        image = ""
        for key in ("image", "filename"):
            raw = ep.get(key)
            if raw:
                image = raw if str(raw).startswith("http") else f"https://artworks.thetvdb.com{raw}"
                break
        rating = None
        for rk in ("siteRating", "rating", "score"):
            if ep.get(rk) is not None:
                try:
                    rating = float(ep[rk])
                    break
                except (TypeError, ValueError):
                    continue
        out = {
            "episode_title": name if name and not _is_mostly_cjk(name) else "",
            "episode_overview": overview if overview and not _is_mostly_cjk(overview) else "",
            "episode_backdrop": image,
            "episode_released": (ep.get("aired") or ep.get("firstAired") or ""),
            "episode_rating": rating,
        }
        # If absolute resolved S/E, surface them
        if ep.get("seasonNumber") is not None:
            try:
                out["season_number"] = int(ep["seasonNumber"])
            except (TypeError, ValueError):
                pass
        if ep.get("number") is not None:
            try:
                out["episode_number"] = int(ep["number"])
            except (TypeError, ValueError):
                pass
        return out
    except Exception as e:
        LOGGER.debug(f"[KITSU] TVDB episode fields failed: {e}")
        return {}


async def _tmdb_episode_fields(tmdb_id: int, season: int, episode: int) -> dict:
    """Cached TMDB episode fields (language=en-US client). Empty dict if unavailable."""
    try:
        from Backend.helper.metadata.providers import tmdb as tmdb_mod
        from Backend.helper.metadata.common import format_tmdb_image
        if not tmdb_mod.tmdb_api_key():
            return {}
        ep = await tmdb_mod.episode_details(int(tmdb_id), int(season), int(episode))
        if not ep:
            return {}
        name = getattr(ep, "name", None) or ""
        overview = getattr(ep, "overview", None) or ""
        still = getattr(ep, "still_path", None)
        air = getattr(ep, "air_date", None)
        rating = None
        vote = getattr(ep, "vote_average", None)
        if vote is not None:
            try:
                rating = float(vote)
            except (TypeError, ValueError):
                rating = None
        return {
            "episode_title": name if name and not _is_mostly_cjk(name) else "",
            "episode_overview": overview if overview and not _is_mostly_cjk(overview) else "",
            "episode_backdrop": format_tmdb_image(still, "original") if still else "",
            "episode_released": str(air) if air else "",
            "episode_rating": rating,
        }
    except Exception as e:
        LOGGER.debug(f"[KITSU] TMDB episode fields failed: {e}")
        return {}


async def _tmdb_series_english(tmdb_id: int) -> dict:
    """Cached TMDB series-level English overview/title."""
    try:
        from Backend.helper.metadata.providers import tmdb as tmdb_mod
        if not tmdb_mod.tmdb_api_key():
            return {}
        tv = await tmdb_mod.details("tv", int(tmdb_id))
        if not tv:
            return {}
        overview = getattr(tv, "overview", None) or ""
        name = getattr(tv, "name", None) or ""
        return {
            "description": overview if overview and not _is_mostly_cjk(overview) else "",
            "title_english": name if name and not _is_mostly_cjk(name) else "",
        }
    except Exception as e:
        LOGGER.debug(f"[KITSU] TMDB series english failed: {e}")
        return {}


async def _tvdb_series_english(tvdb_id: int) -> dict:
    try:
        from Backend.helper.metadata.providers import tvdb as tvdb_mod
        if not tvdb_mod.tvdb_api_key():
            return {}
        series = await tvdb_mod.series_details(int(tvdb_id))
        if not series:
            return {}
        overview = (series.get("overview") or "").strip()
        name = (series.get("name") or "").strip()
        return {
            "description": overview if overview and not _is_mostly_cjk(overview) else "",
            "title_english": name if name and not _is_mostly_cjk(name) else "",
        }
    except Exception as e:
        LOGGER.debug(f"[KITSU] TVDB series english failed: {e}")
        return {}


def _merge_field(current: str, incoming: str) -> str:
    """Keep current if already good English; else take incoming if better."""
    if not _needs_english(current):
        return current
    if incoming and not _needs_english(incoming):
        return incoming
    if not current and incoming:
        return incoming
    return current or incoming or ""


async def _resolve_episode_meta(
    payload: dict,
    *,
    anizip_ep: dict,
    season_number: int,
    episode_number: int,
    absolute: bool,
    absolute_hint,
    tvdb_id,
    tmdb_id,
) -> dict:
    """Per-field cascade for episode details.

    Order per field:
      1) ani.zip (English)
      2) TVDB (English, cached)
      3) TMDB (English, cached)
      4) ani.zip any language / generic fallback
    """
    # 1) ani.zip English
    az = _anizip_episode_fields(anizip_ep or {})
    title = az.get("episode_title") or ""
    overview = az.get("episode_overview") or ""
    backdrop = az.get("episode_backdrop") or ""
    released = az.get("episode_released") or ""
    rating = az.get("episode_rating")

    need_title = _needs_english(title)
    need_overview = _needs_english(overview)
    need_backdrop = not bool(backdrop)
    need_rating = rating is None

    # 2) TVDB only for missing fields
    if (need_title or need_overview or need_backdrop) and tvdb_id:
        try:
            tvdb_id_int = int(tvdb_id)
        except (TypeError, ValueError):
            tvdb_id_int = None
        if tvdb_id_int:
            tv = await _tvdb_episode_fields(
                tvdb_id_int, season_number, episode_number, absolute_hint=absolute_hint
            )
            if tv.get("season_number") is not None and absolute:
                season_number = int(tv["season_number"])
                payload["season_number"] = season_number
            if tv.get("episode_number") is not None and absolute:
                episode_number = int(tv["episode_number"])
                payload["episode_number"] = episode_number
            title = _merge_field(title, tv.get("episode_title") or "")
            overview = _merge_field(overview, tv.get("episode_overview") or "")
            if need_backdrop and tv.get("episode_backdrop"):
                backdrop = tv["episode_backdrop"]
                need_backdrop = False
            if need_rating and tv.get("episode_rating") is not None:
                rating = tv["episode_rating"]
                need_rating = False
            released = released or tv.get("episode_released") or ""
            need_title = _needs_english(title)
            need_overview = _needs_english(overview)

    # 3) TMDB only for still-missing fields
    if (need_title or need_overview or need_backdrop) and tmdb_id:
        try:
            tmdb_id_int = int(tmdb_id)
        except (TypeError, ValueError):
            tmdb_id_int = None
        if tmdb_id_int and int(season_number or 0) > 0:
            tm = await _tmdb_episode_fields(tmdb_id_int, season_number, episode_number)
            title = _merge_field(title, tm.get("episode_title") or "")
            overview = _merge_field(overview, tm.get("episode_overview") or "")
            if need_backdrop and tm.get("episode_backdrop"):
                backdrop = tm["episode_backdrop"]
                need_backdrop = False
            if need_rating and tm.get("episode_rating") is not None:
                rating = tm["episode_rating"]
                need_rating = False
            released = released or tm.get("episode_released") or ""
            need_title = _needs_english(title)
            need_overview = _needs_english(overview)

    # 4) Last resort: ani.zip any language (ja etc.)
    if need_title:
        title = _pick_english_text((anizip_ep or {}).get("title"), allow_fallback=True) or title
    if need_overview:
        title_ov = _pick_english_text(
            (anizip_ep or {}).get("overview"),
            (anizip_ep or {}).get("summary"),
            allow_fallback=True,
        )
        overview = overview or title_ov or ""

    if not title:
        title = _episode_title_fallback(season_number, episode_number, absolute=absolute)

    # Last visual fallback: series backdrop/poster if episode still has no still
    if not backdrop:
        backdrop = (payload.get("backdrop") or payload.get("poster") or "") or ""

    payload["episode_title"] = title
    payload["episode_overview"] = overview or ""
    payload["episode_backdrop"] = backdrop or ""
    payload["episode_released"] = released or payload.get("episode_released") or ""
    if rating is not None:
        try:
            payload["episode_rating"] = float(rating)
        except (TypeError, ValueError):
            pass
    return payload


async def _resolve_series_description(payload: dict, tvdb_id, tmdb_id) -> dict:
    """Series description: Kitsu EN first (already in payload), then TVDB, then TMDB, then keep JA."""
    desc = payload.get("description") or ""
    if not _needs_english(desc):
        return payload

    if tvdb_id:
        try:
            tv = await _tvdb_series_english(int(tvdb_id))
            if tv.get("description"):
                payload["description"] = tv["description"]
                desc = payload["description"]
            if tv.get("title_english") and _is_mostly_cjk(payload.get("title") or ""):
                payload["title"] = tv["title_english"]
                payload["title_english"] = tv["title_english"]
        except Exception:
            pass

    if _needs_english(desc) and tmdb_id:
        try:
            tm = await _tmdb_series_english(int(tmdb_id))
            if tm.get("description"):
                payload["description"] = tm["description"]
            if tm.get("title_english"):
                payload["title_english"] = tm["title_english"]
                if _is_mostly_cjk(payload.get("title") or ""):
                    payload["title"] = tm["title_english"]
        except Exception:
            pass
    return payload


def _tvdb_art_url(path: str) -> str:
    if not path:
        return ""
    p = str(path)
    if p.startswith("http"):
        return p
    return f"https://artworks.thetvdb.com{p}" if p.startswith("/") else f"https://artworks.thetvdb.com/{p}"


def _pick_tvdb_artwork(artworks: list, type_ids: set) -> str:
    for art in artworks or []:
        try:
            if int(art.get("type") or 0) in type_ids and art.get("image"):
                return _tvdb_art_url(art["image"])
        except (TypeError, ValueError):
            continue
    return ""


async def _resolve_series_art(payload: dict, tvdb_id, tmdb_id) -> dict:
    """Poster / backdrop / logo fallbacks: Kitsu/ani.zip → TVDB → TMDB.

    Only fills fields that are still empty. All remote calls are cached in providers.
    """
    need_poster = not (payload.get("poster") or "").strip()
    need_backdrop = not (payload.get("backdrop") or "").strip()
    need_logo = not (payload.get("logo") or "").strip()
    need_rate = not payload.get("rate")

    if not (need_poster or need_backdrop or need_logo or need_rate):
        return payload

    # TVDB series
    if tvdb_id and (need_poster or need_backdrop or need_logo or need_rate):
        try:
            from Backend.helper.metadata.providers import tvdb as tvdb_mod
            if tvdb_mod.tvdb_api_key():
                series = await tvdb_mod.series_details(int(tvdb_id))
                if series:
                    artworks = series.get("artworks") or []
                    if need_poster:
                        poster = (
                            _pick_tvdb_artwork(artworks, {2, 14, 27})
                            or _tvdb_art_url(series.get("image") or "")
                        )
                        if poster:
                            payload["poster"] = poster
                            need_poster = False
                    if need_backdrop:
                        backdrop = (
                            _pick_tvdb_artwork(artworks, {3, 15, 19})
                            or _tvdb_art_url(series.get("background") or series.get("fanart") or "")
                        )
                        if backdrop:
                            payload["backdrop"] = backdrop
                            need_backdrop = False
                    if need_logo:
                        logo = _pick_tvdb_artwork(artworks, {25, 23})
                        if logo:
                            payload["logo"] = logo
                            need_logo = False
                    if need_rate:
                        site = series.get("siteRating") or series.get("rating")
                        if site is not None:
                            try:
                                payload["rate"] = float(site)
                                need_rate = False
                            except (TypeError, ValueError):
                                pass
                    payload.setdefault("tvdb_id", int(tvdb_id))
        except Exception as e:
            LOGGER.debug(f"[KITSU] TVDB series art failed: {e}")

    # TMDB series
    if tmdb_id and (need_poster or need_backdrop or need_logo or need_rate):
        try:
            from Backend.helper.metadata.providers import tmdb as tmdb_mod
            from Backend.helper.metadata.common import format_tmdb_image
            if tmdb_mod.tmdb_api_key():
                tv = await tmdb_mod.details("tv", int(tmdb_id))
                if tv:
                    if need_poster and getattr(tv, "poster_path", None):
                        payload["poster"] = format_tmdb_image(tv.poster_path)
                        need_poster = False
                    if need_backdrop and getattr(tv, "backdrop_path", None):
                        payload["backdrop"] = format_tmdb_image(tv.backdrop_path, "original")
                        need_backdrop = False
                    if need_logo:
                        logo = tmdb_mod.get_tmdb_logo(getattr(tv, "images", None))
                        if logo:
                            payload["logo"] = logo
                            need_logo = False
                    if need_rate and getattr(tv, "vote_average", None):
                        try:
                            payload["rate"] = float(tv.vote_average)
                            need_rate = False
                        except (TypeError, ValueError):
                            pass
                    payload.setdefault("tmdb_id", int(tmdb_id))
        except Exception as e:
            LOGGER.debug(f"[KITSU] TMDB series art failed: {e}")

    return payload


async def _enrich_from_tvdb(
    payload: dict,
    tvdb_id: int,
    season_number: int,
    episode_number: int,
    *,
    absolute_hint: int | None = None,
) -> dict:
    """Thin wrapper: series art is handled by _resolve_series_art."""
    return await _resolve_series_art(payload, tvdb_id, payload.get("tmdb_id"))


def _find_anizip_episode(episodes: dict, season, episode, absolute: bool) -> dict:
    """Locate the ani.zip episode entry for an absolute or S/E number.

    When absolute=True, prefer entries that carry absoluteEpisodeNumber so we
    get the rich TVDB-style seasonNumber/episodeNumber instead of the sparse
    AniDB-only keys that often collide with the same numeric string.
    """
    if not episodes:
        return {}
    ep_num = int(episode)

    # When looking up by absolute number, prefer the entry that actually
    # carries absoluteEpisodeNumber (these are the ones with season/episode).
    if absolute or season is None:
        for candidate in episodes.values():
            try:
                if int(candidate.get("absoluteEpisodeNumber") or -1) == ep_num:
                    return candidate
            except (TypeError, ValueError):
                continue

    # 1) Direct key match (AniDB-style sequential / absolute key)
    if str(ep_num) in episodes:
        return episodes[str(ep_num)] or {}

    # 2) Match absoluteEpisodeNumber (also useful when not in absolute mode)
    for candidate in episodes.values():
        try:
            if int(candidate.get("absoluteEpisodeNumber") or -1) == ep_num:
                return candidate
        except (TypeError, ValueError):
            continue

    # 3) Match episode / episodeNumber fields
    for candidate in episodes.values():
        try:
            if int(candidate.get("episodeNumber") or candidate.get("episode") or -1) == ep_num:
                if absolute or season is None:
                    return candidate
                if int(candidate.get("seasonNumber") or candidate.get("season") or -1) == int(season):
                    return candidate
        except (TypeError, ValueError):
            continue

    # 4) Season + relative episode when both known
    if season is not None and not absolute:
        for candidate in episodes.values():
            try:
                if (
                    int(candidate.get("seasonNumber") or -1) == int(season)
                    and int(candidate.get("episodeNumber") or candidate.get("episode") or -1) == ep_num
                ):
                    return candidate
            except (TypeError, ValueError):
                continue
    return {}

def _ids_from_anizip(doc: dict) -> dict:
    """Extract cross-db IDs from ani.zip mappings block."""
    mappings = (doc or {}).get("mappings") or {}
    out = {}
    for key, dest in (
        ("anidb_id", "anidb_id"),
        ("anilist_id", "anilist_id"),
        ("mal_id", "mal_id"),
        ("thetvdb_id", "tvdb_id"),
        ("themoviedb_id", "tmdb_id"),
        ("imdb_id", "imdb_id"),
    ):
        val = mappings.get(key)
        if val is None:
            continue
        if key == "imdb_id":
            out[dest] = str(val).strip() or None
            continue
        try:
            out[dest] = int(val)
        except (TypeError, ValueError):
            continue
    return out

async def fetch_anime_tv(
    title: str,
    season,
    episode: int,
    encoded_string,
    year=None,
    quality=None,
    absolute: bool = False,
) -> dict | None:
    is_abs = bool(absolute)
    row = await search_anime(title, movie=False)
    if not row:
        return None

    kitsu_id = row.get("id")
    doc = await get_anizip_mappings(kitsu_id) if kitsu_id else {}
    episodes = (doc or {}).get("episodes") or {}
    extra_ids = _ids_from_anizip(doc)

    ep = _find_anizip_episode(episodes, season, episode, absolute=is_abs)

    use_season = season
    use_episode = int(episode)

    if is_abs:
        if ep.get("seasonNumber") is not None:
            try:
                use_season = int(ep["seasonNumber"])
            except (TypeError, ValueError):
                pass
        if ep.get("episodeNumber") is not None:
            try:
                use_episode = int(ep["episodeNumber"])
            except (TypeError, ValueError):
                pass
        elif ep.get("absoluteEpisodeNumber") is not None:
            try:
                use_episode = int(ep["absoluteEpisodeNumber"])
            except (TypeError, ValueError):
                pass

    needs_fallback = is_abs and (
        not ep
        or ep.get("seasonNumber") is None
        or (
            ep.get("absoluteEpisodeNumber") is None
            and ep.get("seasonNumber") is None
        )
    )

    if needs_fallback:
        try:
            from Backend.helper.metadata.episode_maps import resolve_absolute_episode

            map_hit = await resolve_absolute_episode(
                int(episode),
                anidb_id=extra_ids.get("anidb_id"),
                anilist_id=extra_ids.get("anilist_id"),
                mal_id=extra_ids.get("mal_id"),
            )
        except Exception as e:
            LOGGER.debug(f"[KITSU] episode_maps resolve failed: {e}")
            map_hit = None

        if map_hit:
            if map_hit.get("season_number") is not None:
                use_season = int(map_hit["season_number"])
            if map_hit.get("episode_number") is not None:
                use_episode = int(map_hit["episode_number"])
            for key in ("tvdb_id", "tmdb_id", "imdb_id"):
                if map_hit.get(key) and not extra_ids.get(key):
                    extra_ids[key] = map_hit[key]
            if map_hit.get("tvdb_absolute") and map_hit.get("tvdb_id"):
                extra_ids["tvdb_id"] = map_hit["tvdb_id"]

    season_number = int(use_season) if use_season is not None else 1
    episode_number = int(use_episode)

    if is_abs and (not ep or ep.get("seasonNumber") is None) and season_number == 1 and episode_number == int(episode):
        LOGGER.info(
            f"[KITSU] Absolute episode {episode} not fully mapped for '{title}' "
            f"(kitsu={kitsu_id}) — indexing with S{season_number}E{episode_number}"
        )

    attrs = row.get("attributes") or {}
    display_title = (
        (attrs.get("titles") or {}).get("en")
        or (attrs.get("titles") or {}).get("en_jp")
        or attrs.get("canonicalTitle")
        or title
    )
    payload = _common_payload(row, doc or {}, display_title)
    for key, val in extra_ids.items():
        if val and not payload.get(key):
            payload[key] = val

    payload.update({
        "media_type": "tv",
        "season_number": season_number,
        "episode_number": episode_number,
        "quality": quality,
        "encoded_string": encoded_string,
        "absolute_episode": int(episode) if is_abs else None,
    })

    # Series description: Kitsu EN → TVDB → TMDB → JA
    payload = await _resolve_series_description(
        payload,
        payload.get("tvdb_id") or extra_ids.get("tvdb_id"),
        payload.get("tmdb_id") or extra_ids.get("tmdb_id"),
    )

    # Series art / rating: Kitsu → ani.zip (already in payload) → TVDB → TMDB
    tvdb_id = payload.get("tvdb_id") or extra_ids.get("tvdb_id")
    tmdb_id = payload.get("tmdb_id") or extra_ids.get("tmdb_id")
    try:
        payload = await _resolve_series_art(payload, tvdb_id, tmdb_id)
    except Exception as e:
        LOGGER.debug(f"[KITSU] series art skip: {e}")

    # Episode fields: ani.zip EN → TVDB → TMDB → JA (per field, cached APIs)
    abs_hint = int(episode) if is_abs else None
    payload = await _resolve_episode_meta(
        payload,
        anizip_ep=ep or {},
        season_number=int(payload.get("season_number") or season_number),
        episode_number=int(payload.get("episode_number") or episode_number),
        absolute=is_abs,
        absolute_hint=abs_hint,
        tvdb_id=payload.get("tvdb_id") or extra_ids.get("tvdb_id"),
        tmdb_id=payload.get("tmdb_id") or extra_ids.get("tmdb_id"),
    )

    return payload



async def fetch_anime_movie(title, encoded_string, year=None, quality=None) -> Optional[dict]:
    row = await search_anime(title, movie=True)
    if not row:
        # also try without subtype filter
        row = await search_anime(title, movie=False)
        if row:
            subtype = ((row.get("attributes") or {}).get("subtype") or "").lower()
            if subtype not in ("movie", "special", "ova", "ona", ""):
                LOGGER.info(f"[KITSU] No movie match for '{title}'")
                return None
    if not row:
        LOGGER.info(f"[KITSU] No movie match for '{title}'")
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    payload = _common_payload(row, doc, title)
    payload.update({"media_type": "movie", "quality": quality, "encoded_string": encoded_string})
    return payload
