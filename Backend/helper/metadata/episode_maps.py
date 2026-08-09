"""Anime episode number mapping fallbacks.

Resolution order used by Kitsu absolute-episode path when ani.zip lacks
seasonNumber/episodeNumber:

  1. Anime-Lists XML  (AniDB → TVDB/TMDB S/E + offsets)
  2. anibridge-mappings (daily JSON range maps)

Both datasets are downloaded once and cached in-process (and optionally on
disk under the working directory). Refresh is lazy + TTL-based.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from Backend.logger import LOGGER

ANIME_LISTS_URL = (
    "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/"
    "anime-list-master.xml"
)
ANIBRIDGE_ZST_URL = (
    "https://github.com/anibridge/anibridge-mappings/releases/download/v3/"
    "mappings.json.zst"
)
ANIBRIDGE_MIN_URL = (
    "https://github.com/anibridge/anibridge-mappings/releases/download/v3/"
    "mappings.min.json"
)

_TTL_SECONDS = 24 * 3600

_CACHE_DIR = os.environ.get(
    "EPISODE_MAPS_CACHE",
    os.path.join(os.path.dirname(__file__), ".episode_maps_cache"),
)


@dataclass
class AnimeListEntry:
    anidb_id: int
    tvdb_id: Optional[int] = None
    tmdb_tv_id: Optional[int] = None
    default_tvdb_season: Optional[str] = None
    episode_offset: int = 0
    tmdb_season: Optional[str] = None
    tmdb_offset: int = 0
    imdb_id: Optional[str] = None
    name: str = ""
    mappings: List[Tuple[int, int, Optional[int], Optional[int], int]] = field(
        default_factory=list
    )
    special_map: Dict[int, int] = field(default_factory=dict)


_anime_lists: Dict[int, AnimeListEntry] = {}
_anime_lists_by_imdb: Dict[str, List[AnimeListEntry]] = {}
_anime_lists_loaded_at: float = 0.0
_anime_lists_lock = asyncio.Lock()

_anibridge: dict = {}
_anibridge_loaded_at: float = 0.0
_anibridge_lock = asyncio.Lock()

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        return _client


def _ensure_cache_dir() -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except Exception:
        pass


def _read_disk(name: str) -> Optional[bytes]:
    path = os.path.join(_CACHE_DIR, name)
    try:
        if not os.path.isfile(path):
            return None
        if time.time() - os.path.getmtime(path) > _TTL_SECONDS:
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _write_disk(name: str, data: bytes) -> None:
    try:
        _ensure_cache_dir()
        with open(os.path.join(_CACHE_DIR, name), "wb") as f:
            f.write(data)
    except Exception as e:
        LOGGER.debug(f"[EP_MAPS] disk cache write failed: {e}")


def _parse_anime_lists_xml(raw: bytes) -> Dict[int, AnimeListEntry]:
    out: Dict[int, AnimeListEntry] = {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        LOGGER.warning(f"[EP_MAPS] Anime-Lists XML parse error: {e}")
        return out

    for node in root.findall("anime"):
        try:
            anidb_id = int(node.get("anidbid") or 0)
        except (TypeError, ValueError):
            continue
        if not anidb_id:
            continue

        def _int_or_none(val):
            if val is None or str(val).strip() in ("", "unknown", "movie"):
                return None
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        tvdb_raw = (node.get("tvdbid") or "").strip()
        tvdb_id = _int_or_none(tvdb_raw) if tvdb_raw not in ("movie", "unknown") else None
        tmdb_tv = _int_or_none(node.get("tmdbtv"))
        default_season = (node.get("defaulttvdbseason") or "").strip() or None
        try:
            ep_off = int(node.get("episodeoffset") or 0)
        except (TypeError, ValueError):
            ep_off = 0
        tmdb_season = (node.get("tmdbseason") or "").strip() or None
        try:
            tmdb_off = int(node.get("tmdboffset") or 0)
        except (TypeError, ValueError):
            tmdb_off = 0
        imdb = (node.get("imdbid") or "").strip() or None
        name_el = node.find("name")
        name = (name_el.text or "").strip() if name_el is not None else ""

        entry = AnimeListEntry(
            anidb_id=anidb_id,
            tvdb_id=tvdb_id,
            tmdb_tv_id=tmdb_tv,
            default_tvdb_season=default_season,
            episode_offset=ep_off,
            tmdb_season=tmdb_season,
            tmdb_offset=tmdb_off,
            imdb_id=imdb,
            name=name,
        )

        ml = node.find("mapping-list")
        if ml is not None:
            for m in ml.findall("mapping"):
                try:
                    anidb_season = int(m.get("anidbseason") or 1)
                except (TypeError, ValueError):
                    anidb_season = 1
                tvdb_season_raw = m.get("tvdbseason")
                tmdb_season_raw = m.get("tmdbseason")
                text = (m.text or "").strip()

                if text.startswith(";") and "start" not in m.attrib:
                    for pair in text.strip(";").split(";"):
                        pair = pair.strip()
                        if not pair or "-" not in pair:
                            continue
                        a, b = pair.split("-", 1)
                        try:
                            entry.special_map[int(a)] = int(b)
                        except (TypeError, ValueError):
                            continue
                    continue

                try:
                    start = int(m.get("start")) if m.get("start") not in (None, "") else None
                    end = int(m.get("end")) if m.get("end") not in (None, "") else None
                    offset = int(m.get("offset") or 0)
                except (TypeError, ValueError):
                    start, end, offset = None, None, 0

                season_target = None
                if tvdb_season_raw not in (None, ""):
                    try:
                        season_target = int(tvdb_season_raw)
                    except (TypeError, ValueError):
                        season_target = None
                elif tmdb_season_raw not in (None, ""):
                    try:
                        season_target = int(tmdb_season_raw)
                    except (TypeError, ValueError):
                        season_target = None

                if season_target is not None:
                    entry.mappings.append(
                        (anidb_season, season_target, start, end, offset)
                    )

        out[anidb_id] = entry
    return out


def _normalize_imdb(imdb_id: Optional[str]) -> Optional[str]:
    if not imdb_id:
        return None
    s = str(imdb_id).strip().lower()
    if not s:
        return None
    if s.startswith("tt"):
        return s
    if s.isdigit():
        return f"tt{s}"
    return s


def _rebuild_imdb_index(parsed: Dict[int, AnimeListEntry]) -> Dict[str, List[AnimeListEntry]]:
    index: Dict[str, List[AnimeListEntry]] = {}
    for entry in parsed.values():
        raw = entry.imdb_id or ""
        for part in re.split(r"[,;\s]+", raw):
            key = _normalize_imdb(part)
            if not key:
                continue
            index.setdefault(key, []).append(entry)
    return index


async def ensure_anime_lists() -> Dict[int, AnimeListEntry]:
    global _anime_lists, _anime_lists_by_imdb, _anime_lists_loaded_at
    async with _anime_lists_lock:
        if _anime_lists and (time.time() - _anime_lists_loaded_at) < _TTL_SECONDS:
            return _anime_lists

        raw = _read_disk("anime-list-master.xml")
        if raw is None:
            try:
                client = await _get_client()
                resp = await client.get(ANIME_LISTS_URL)
                if resp.status_code == 200 and resp.content:
                    raw = resp.content
                    _write_disk("anime-list-master.xml", raw)
                    LOGGER.info(
                        f"[EP_MAPS] Downloaded Anime-Lists XML ({len(raw)} bytes)"
                    )
            except Exception as e:
                LOGGER.warning(f"[EP_MAPS] Anime-Lists download failed: {e}")

        if raw:
            parsed = _parse_anime_lists_xml(raw)
            if parsed:
                _anime_lists = parsed
                _anime_lists_by_imdb = _rebuild_imdb_index(parsed)
                _anime_lists_loaded_at = time.time()
                LOGGER.info(
                    f"[EP_MAPS] Anime-Lists loaded: {len(parsed)} entries, "
                    f"{len(_anime_lists_by_imdb)} imdb keys"
                )
        return _anime_lists


def resolve_via_anime_lists(
    anidb_id: Optional[int],
    absolute: int,
    *,
    prefer_tvdb: bool = True,
) -> Optional[dict]:
    """Map absolute/AniDB regular episode → season + episode via Anime-Lists."""
    if not anidb_id or absolute < 1:
        return None
    entry = _anime_lists.get(int(anidb_id))
    if not entry:
        return None

    result: dict = {
        "tvdb_id": entry.tvdb_id,
        "tmdb_id": entry.tmdb_tv_id,
        "imdb_id": entry.imdb_id,
        "source": "anime-lists",
    }

    # 1) Explicit range mappings FIRST — even when defaulttvdbseason is "a".
    #    One Piece ships both "a" AND per-arc season ranges; ranges give real
    #    S/E (e.g. abs 1171 → S23E16) without needing the TVDB API.
    for anidb_season, tvdb_season, start, end, offset in entry.mappings:
        if anidb_season not in (0, 1):
            continue
        if start is not None and absolute < start:
            continue
        if end is not None and absolute > end:
            continue
        ep = absolute + int(offset or 0)
        if ep < 1:
            continue
        result["season_number"] = int(tvdb_season)
        result["episode_number"] = ep
        return result

    # 2) defaulttvdbseason == "a" → absolute on TVDB (caller may resolve via API)
    if (entry.default_tvdb_season or "").lower() == "a":
        result["absolute_episode"] = absolute
        result["season_number"] = None
        result["episode_number"] = absolute
        result["tvdb_absolute"] = True
        return result

    # 3) Simple default season + offset
    try:
        season = int(entry.default_tvdb_season) if entry.default_tvdb_season else 1
    except (TypeError, ValueError):
        season = 1
    ep = absolute + (entry.episode_offset or 0)
    if ep < 1:
        return None
    result["season_number"] = season
    result["episode_number"] = ep
    return result


_RANGE_RE = re.compile(
    r"^(?P<start>\d+)(?:-(?P<end>\d+)?)?(?:\|(?P<ratio>-?\d+(?:\.\d+)?))?$"
)


def _parse_range_token(token: str) -> Optional[Tuple[int, Optional[int], float]]:
    token = (token or "").strip()
    if not token:
        return None
    m = _RANGE_RE.match(token)
    if not m:
        if token.isdigit():
            n = int(token)
            return n, n, 1.0
        return None
    start = int(m.group("start"))
    end_raw = m.group("end")
    if end_raw is None and "-" in token and not token.endswith("|"):
        end = None
    elif end_raw is None and "-" not in token.split("|")[0]:
        end = start
    else:
        end = int(end_raw) if end_raw not in (None, "") else None
    ratio = float(m.group("ratio") or 1.0)
    return start, end, ratio


def _map_through_ranges(source_ep: int, range_map: dict) -> Optional[int]:
    if not isinstance(range_map, dict):
        return None
    for src_key, tgt_val in range_map.items():
        src = _parse_range_token(str(src_key))
        if not src:
            continue
        s_start, s_end, _ = src
        if source_ep < s_start:
            continue
        if s_end is not None and source_ep > s_end:
            continue
        offset_in_src = source_ep - s_start
        tgt_str = str(tgt_val or "").strip()
        if not tgt_str:
            return source_ep
        segments = [p.strip() for p in tgt_str.split(",") if p.strip()]
        remaining = offset_in_src
        for seg in segments:
            parsed = _parse_range_token(seg)
            if not parsed:
                continue
            t_start, t_end, ratio = parsed
            if ratio == 0:
                continue
            if ratio > 0:
                span = 1
                if t_end is not None:
                    tgt_count = t_end - t_start + 1
                    span = max(1, int(round(tgt_count / ratio)))
                if remaining < span or t_end is None:
                    return t_start + int(remaining * ratio)
                remaining -= span
            else:
                inv = abs(ratio)
                span = int(inv)
                if remaining < span or t_end is None:
                    return t_start + int(remaining / inv)
                remaining -= span
    return None


def _descriptor_parts(desc: str) -> Tuple[str, str, Optional[str]]:
    parts = str(desc).split(":")
    provider = parts[0] if parts else ""
    id_ = parts[1] if len(parts) > 1 else ""
    scope = parts[2] if len(parts) > 2 else None
    return provider, id_, scope


async def ensure_anibridge() -> dict:
    global _anibridge, _anibridge_loaded_at
    async with _anibridge_lock:
        if _anibridge and (time.time() - _anibridge_loaded_at) < _TTL_SECONDS:
            return _anibridge

        data = None
        raw = _read_disk("mappings.json.zst")
        if raw is None:
            try:
                client = await _get_client()
                resp = await client.get(ANIBRIDGE_ZST_URL)
                if resp.status_code == 200 and resp.content:
                    raw = resp.content
                    _write_disk("mappings.json.zst", raw)
            except Exception as e:
                LOGGER.debug(f"[EP_MAPS] anibridge zst download failed: {e}")

        if raw:
            try:
                import zstandard as zstd
                dctx = zstd.ZstdDecompressor()
                data = json.loads(dctx.decompress(raw))
            except Exception:
                data = None

        if data is None:
            raw_json = _read_disk("mappings.min.json")
            if raw_json is None:
                try:
                    client = await _get_client()
                    resp = await client.get(ANIBRIDGE_MIN_URL)
                    if resp.status_code == 200 and resp.content:
                        raw_json = resp.content
                        _write_disk("mappings.min.json", raw_json)
                        LOGGER.info(
                            f"[EP_MAPS] Downloaded anibridge mappings ({len(raw_json)} bytes)"
                        )
                except Exception as e:
                    LOGGER.warning(f"[EP_MAPS] anibridge JSON download failed: {e}")
            if raw_json:
                try:
                    data = json.loads(raw_json)
                except Exception as e:
                    LOGGER.warning(f"[EP_MAPS] anibridge JSON parse failed: {e}")

        if isinstance(data, dict):
            data.pop("$meta", None)
            _anibridge = data
            _anibridge_loaded_at = time.time()
            LOGGER.info(f"[EP_MAPS] anibridge loaded: {len(data)} source descriptors")
        return _anibridge


def resolve_via_anibridge(
    *,
    anidb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    mal_id: Optional[int] = None,
    absolute: int,
) -> Optional[dict]:
    if absolute < 1 or not _anibridge:
        return None

    candidates: List[str] = []
    if anidb_id:
        candidates.append(f"anidb:{int(anidb_id)}:R")
        candidates.append(f"anidb:{int(anidb_id)}")
    if anilist_id:
        candidates.append(f"anilist:{int(anilist_id)}")
    if mal_id:
        candidates.append(f"mal:{int(mal_id)}")

    for src_desc in candidates:
        targets = _anibridge.get(src_desc)
        if not isinstance(targets, dict):
            continue
        ordered = sorted(
            targets.items(),
            key=lambda kv: (
                0 if str(kv[0]).startswith("tvdb_show:") else
                1 if str(kv[0]).startswith("tmdb_show:") else 2
            ),
        )
        for tgt_desc, range_map in ordered:
            provider, id_, scope = _descriptor_parts(tgt_desc)
            if provider not in ("tvdb_show", "tmdb_show"):
                continue
            if not id_ or not str(id_).isdigit():
                continue
            season = None
            if scope and scope.startswith("s") and scope[1:].isdigit():
                season = int(scope[1:])
            mapped_ep = _map_through_ranges(absolute, range_map or {})
            if mapped_ep is None and not range_map:
                mapped_ep = absolute
            if mapped_ep is None or mapped_ep < 1:
                continue
            result = {
                "season_number": season if season is not None else 1,
                "episode_number": int(mapped_ep),
                "source": "anibridge",
            }
            if provider == "tvdb_show":
                result["tvdb_id"] = int(id_)
            else:
                result["tmdb_id"] = int(id_)
            return result
    return None


async def resolve_absolute_episode(
    absolute: int,
    *,
    anidb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    mal_id: Optional[int] = None,
) -> Optional[dict]:
    if absolute < 1:
        return None

    await ensure_anime_lists()
    hit = resolve_via_anime_lists(anidb_id, absolute)
    if hit:
        if hit.get("tvdb_absolute") and hit.get("tvdb_id"):
            return hit
        if hit.get("season_number") is not None and hit.get("episode_number") is not None:
            LOGGER.info(
                f"[EP_MAPS] Anime-Lists: abs={absolute} anidb={anidb_id} "
                f"→ S{hit['season_number']}E{hit['episode_number']} "
                f"(tvdb={hit.get('tvdb_id')})"
            )
            return hit
        if hit.get("tvdb_absolute"):
            return hit

    await ensure_anibridge()
    hit = resolve_via_anibridge(
        anidb_id=anidb_id,
        anilist_id=anilist_id,
        mal_id=mal_id,
        absolute=absolute,
    )
    if hit:
        LOGGER.info(
            f"[EP_MAPS] anibridge: abs={absolute} "
            f"→ S{hit.get('season_number')}E{hit.get('episode_number')} "
            f"(tvdb={hit.get('tvdb_id')} tmdb={hit.get('tmdb_id')})"
        )
        return hit

    return None


def _absolute_from_entry(entry: AnimeListEntry, season: int, episode: int) -> Optional[int]:
    if episode < 1:
        return None
    season = int(season)
    episode = int(episode)

    for anidb_season, tvdb_season, start, end, offset in entry.mappings:
        try:
            if int(tvdb_season) != season:
                continue
        except (TypeError, ValueError):
            continue
        abs_ep = episode - int(offset or 0)
        if abs_ep < 1:
            continue
        if start is not None and abs_ep < int(start):
            continue
        if end is not None and abs_ep > int(end):
            continue
        return abs_ep

    default = (entry.default_tvdb_season or "").strip().lower()
    if default == "a":
        return None

    try:
        default_season = int(default) if default else 1
    except (TypeError, ValueError):
        default_season = 1
    if season == default_season:
        abs_ep = episode - int(entry.episode_offset or 0)
        if abs_ep >= 1:
            return abs_ep

    if season == 1 and not entry.mappings:
        abs_ep = episode - int(entry.episode_offset or 0)
        if abs_ep >= 1:
            return abs_ep
    return None


def _norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[:\-–—_/\\]+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


async def lookup_anime_entries_by_imdb(imdb_id: str) -> List[AnimeListEntry]:
    key = _normalize_imdb(imdb_id)
    if not key:
        return []
    await ensure_anime_lists()
    return list(_anime_lists_by_imdb.get(key) or [])


async def lookup_anime_entries_by_title(title: str) -> List[AnimeListEntry]:
    q = _norm_title(title)
    if not q or len(q) < 3:
        return []
    q_tokens = [t for t in q.split() if len(t) >= 3]
    if not q_tokens:
        return []
    q_token_set = set(q_tokens)
    await ensure_anime_lists()
    exact: List[AnimeListEntry] = []
    strong: List[AnimeListEntry] = []
    for entry in _anime_lists.values():
        name = _norm_title(entry.name)
        if not name or len(name) < 3:
            continue
        if name == q:
            exact.append(entry)
            continue
        name_tokens = [t for t in name.split() if len(t) >= 3]
        if not name_tokens:
            continue
        name_token_set = set(name_tokens)
        if q_token_set == name_token_set:
            strong.append(entry)
            continue
        if len(name_tokens) >= 2 and name_token_set <= q_token_set:
            strong.append(entry)
            continue
        if len(q_tokens) >= 2 and q_token_set <= name_token_set:
            strong.append(entry)
            continue
        if len(name) >= 6 and len(q) >= 6 and (name in q or q in name):
            strong.append(entry)
    return exact or strong


async def is_anime_imdb(imdb_id: str, title: Optional[str] = None) -> bool:
    if await lookup_anime_entries_by_imdb(imdb_id):
        return True
    if title and await lookup_anime_entries_by_title(title):
        return True
    return False


def absolute_from_cinemeta_videos(videos: list, season: int, episode: int) -> Optional[int]:
    if not videos:
        return None
    try:
        season = int(season)
        episode = int(episode)
    except (TypeError, ValueError):
        return None
    if season < 1 or episode < 1:
        return None

    by_season: Dict[int, set] = {}
    for v in videos:
        try:
            s = int(v.get("season"))
            e = int(v.get("episode"))
        except (TypeError, ValueError):
            continue
        if s < 1 or e < 1:
            continue
        by_season.setdefault(s, set()).add(e)

    if season not in by_season or episode not in by_season[season]:
        prior = sum(len(by_season[s]) for s in by_season if s < season)
        return prior + episode if prior or season == 1 else None

    absolute = 0
    for s in sorted(by_season.keys()):
        if s < season:
            absolute += len(by_season[s])
        elif s == season:
            for e in sorted(by_season[s]):
                absolute += 1
                if e == episode:
                    return absolute
            break
    return None


async def absolute_from_imdb_episode(
    imdb_id: str,
    season: int,
    episode: int,
    *,
    title: Optional[str] = None,
    videos: Optional[list] = None,
) -> Optional[dict]:
    try:
        season = int(season)
        episode = int(episode)
    except (TypeError, ValueError):
        return None
    if season < 0 or episode < 1:
        return None

    entries = await lookup_anime_entries_by_imdb(imdb_id)
    source = "anime-lists"
    if not entries and title:
        entries = await lookup_anime_entries_by_title(title)
        source = "anime-lists-title"

    if not entries:
        return None

    for entry in entries:
        abs_ep = _absolute_from_entry(entry, season, episode)
        if abs_ep is not None:
            LOGGER.info(
                f"[EP_MAPS] {imdb_id or title} S{season:02d}E{episode:02d} "
                f"→ abs={abs_ep} (anidb={entry.anidb_id}, {entry.name})"
            )
            return {
                "absolute_episode": int(abs_ep),
                "anidb_id": entry.anidb_id,
                "tvdb_id": entry.tvdb_id,
                "tmdb_id": entry.tmdb_tv_id,
                "imdb_id": _normalize_imdb(imdb_id),
                "name": entry.name,
                "source": source,
                "is_anime": True,
            }

    abs_ep = absolute_from_cinemeta_videos(videos or [], season, episode)
    if abs_ep is not None:
        LOGGER.info(
            f"[EP_MAPS] {imdb_id or title} S{season:02d}E{episode:02d} "
            f"→ abs={abs_ep} via cinemeta cumulative ({entries[0].name})"
        )
        return {
            "absolute_episode": int(abs_ep),
            "anidb_id": entries[0].anidb_id,
            "tvdb_id": entries[0].tvdb_id,
            "tmdb_id": entries[0].tmdb_tv_id,
            "imdb_id": _normalize_imdb(imdb_id),
            "name": entries[0].name,
            "source": "cinemeta-cumulative",
            "is_anime": True,
        }

    if season == 1:
        return {
            "absolute_episode": int(episode),
            "anidb_id": entries[0].anidb_id,
            "tvdb_id": entries[0].tvdb_id,
            "tmdb_id": entries[0].tmdb_tv_id,
            "imdb_id": _normalize_imdb(imdb_id),
            "name": entries[0].name,
            "source": "anime-lists-fallback",
            "is_anime": True,
        }

    return {"is_anime": True, "absolute_episode": None, "source": source, "name": entries[0].name}
