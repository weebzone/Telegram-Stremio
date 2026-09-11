"""Shared helpers for metadata providers and resolvers."""
from __future__ import annotations

import asyncio
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, Optional
from urllib.parse import quote

from rapidfuzz import fuzz

from Backend.logger import LOGGER

# Match thresholds
CINEMETA_THRESHOLD = 0.60
# Lowered from 0.55 -> 0.50 so Spanish/MX titles that already clear the hard
# 0.50 cut in score_candidate() are no longer discarded by a stricter threshold.
TMDB_THRESHOLD = 0.50
TVDB_THRESHOLD = 0.55
KITSU_THRESHOLD = 0.55
STRONG_MATCH = 0.92
ALT_TITLE_LOOKUPS = 5

# ── Latino / Spanish multilingual matching ────────────────────────────────────
# Preferred display + search languages for the Latin community. The TMDB client
# requests es-MX so search results return the Spanish title as the primary
# `title`, and alternative-title lookups keep es-MX / es-ES / en aliases.
PREFERRED_LANG_CODES = ("es-MX", "es-ES", "es", "en-US", "en")

# Combined-file constants (Specials season)
COMBINED_SEASON = 0
COMBINED_EPISODE_BASE = 1000

GRADIENT_COVER_BASE = "https://gradient-cover-api.vercel.app"

API_SEMAPHORE = asyncio.Semaphore(12)

# Shared caches (provider modules may also keep their own)
IMDB_CACHE: dict = {}
TMDB_SEARCH_CACHE: dict = {}
TMDB_DETAILS_CACHE: dict = {}
EPISODE_CACHE: dict = {}
ALT_TITLES_CACHE: dict = {}
TVDB_CACHE: dict = {}
KITSU_CACHE: dict = {}

_INFLIGHT: Dict[tuple, asyncio.Future] = {}

_APOSTROPHE_RE = re.compile(r"['\u2018\u2019`\u00B4]")
_SYMBOL_STRIP_RE = re.compile(r"[&.\-:]+")
_HTML_RE = re.compile(r"<[^>]+>")

# Spanish / Latin diacritics + ¿¡ — used to detect an ES-language title so the
# matcher can give a small boost when query and candidate share the language
# (e.g. "El Señor de los Anillos" vs the Spanish TMDB title).
_ES_DIACRITIC_RE = re.compile(r"[\u00E1\u00E9\u00ED\u00F3\u00FA\u00F1\u00FC\u00C1\u00C9\u00CD\u00D3\u00DA\u00D1\u00DC¿¡]")


def looks_spanish(text: str) -> bool:
    """Heuristic: True when the string carries Spanish diacritics / marks."""
    return bool(_ES_DIACRITIC_RE.search(text or ""))


async def cached_call(store: dict, key, ns: str, producer):
    if key in store:
        return store[key]
    flight_key = (ns, key)
    fut = _INFLIGHT.get(flight_key)
    if fut is not None:
        return await fut
    fut = asyncio.get_running_loop().create_future()
    _INFLIGHT[flight_key] = fut
    try:
        result = await producer()
    except Exception as e:
        _INFLIGHT.pop(flight_key, None)
        if not fut.done():
            fut.set_exception(e)
            fut.exception()
        raise
    store[key] = result
    _INFLIGHT.pop(flight_key, None)
    if not fut.done():
        fut.set_result(result)
    return result


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", _HTML_RE.sub(" ", text)).strip()


def normalize_title(title: str) -> str:
    if not title:
        return ""
    # NFC so composed accents (e.g. 'más') compare identically to any
    # decomposed/accented forms coming from TMDB or the filename.
    t = unicodedata.normalize("NFC", title)
    t = t.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def fuzzy_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        set_ratio = fuzz.token_set_ratio(a, b) / 100.0
        sort_ratio = fuzz.token_sort_ratio(a, b) / 100.0
        a_tokens, b_tokens = a.split(), b.split()
        coverage = (
            min(len(a_tokens), len(b_tokens)) / max(len(a_tokens), len(b_tokens))
            if a_tokens and b_tokens
            else 0.0
        )
        score = max(sort_ratio, set_ratio * coverage)
        # Language boost: when both sides look Spanish (diacritics / ¿¡) the
        # fuzzy tokens can still diverge on word order, so nudge the score so a
        # genuine ES<->ES match clears the threshold instead of being dropped.
        if looks_spanish(a) and looks_spanish(b) and score >= 0.40:
            score = min(1.0, score + 0.10)
        return score
    except Exception:
        return SequenceMatcher(None, a, b).ratio()


def title_similarity(t1: str, t2: str) -> float:
    n1, n2 = normalize_title(t1), normalize_title(t2)
    return fuzzy_ratio(n1, n2) if n1 and n2 else 0.0


def year_from_str(year_val) -> int:
    if not year_val:
        return 0
    m = re.search(r"(\d{4})", str(year_val))
    return int(m.group(1)) if m else 0


def strip_symbols(text: str) -> str:
    if not text:
        return ""
    text = _APOSTROPHE_RE.sub("", text)
    text = _SYMBOL_STRIP_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_candidate(
    query_title: str,
    query_year: Optional[int],
    result_title: str,
    result_year: int,
    year_reliable: bool = True,
    year_lower_bound: bool = False,
) -> float:
    score = title_similarity(query_title, result_title)
    if score < 0.5:
        return score

    if query_year and result_year:
        if year_lower_bound:
            if int(query_year) >= result_year and score >= 0.80:
                score += 0.15 / (1 + (int(query_year) - result_year) * 0.1)
            return score
        diff = abs(int(query_year) - result_year)
        if year_reliable:
            if diff > 2:
                score = max(0.0, score - 0.10 * (diff - 2))
            elif score >= 0.80:
                if diff == 0:
                    score = min(1.0, score + 0.20)
                elif diff == 1:
                    score = min(1.0, score + 0.07)
        elif diff == 0 and score >= 0.80:
            score = min(1.0, score + 0.05)
    elif query_year and year_reliable and not year_lower_bound:
        score = max(0.0, score - 0.20)
    return score



def collect_title_aliases(*groups) -> list:
    """Flatten title / alias fields from provider payloads into unique strings."""
    out: list = []
    seen: set = set()
    for group in groups:
        if group is None:
            continue
        if isinstance(group, str):
            items = [group]
        elif isinstance(group, dict):
            # translations / titles maps: use all values
            items = list(group.values())
        elif isinstance(group, (list, tuple, set)):
            items = []
            for x in group:
                if isinstance(x, dict):
                    # TVDB-style {"name": "..."} or {"title": "..."}
                    items.append(x.get("name") or x.get("title") or x.get("alias") or "")
                else:
                    items.append(x)
        else:
            items = [str(group)]
        for raw in items:
            t = str(raw or "").strip()
            if not t:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    return out


def score_candidate_aliases(
    query_title: str,
    query_year: Optional[int],
    primary_title: str,
    result_year: int,
    aliases=None,
    year_reliable: bool = True,
    year_lower_bound: bool = False,
) -> float:
    """Score a candidate using primary title + all aliases (best match wins).

    Used by TVDB / TMDB / Kitsu / Cinemeta so alternate names, translations,
    and abbreviations participate in matching — not only the canonical title.
    """
    titles = collect_title_aliases(primary_title, aliases)
    if not titles:
        return 0.0
    best = 0.0
    for t in titles:
        s = score_candidate(
            query_title, query_year, t, result_year,
            year_reliable=year_reliable,
            year_lower_bound=year_lower_bound,
        )
        if s > best:
            best = s
            if best >= STRONG_MATCH:
                break
    return best


def build_query_variants(title: str, year: Optional[int] = None) -> list:
    variants = [title]
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
    ordered = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            ordered.append(v)
    return ordered


def first(value):
    return value[0] if isinstance(value, list) else value


def format_runtime(minutes) -> str:
    return f"{minutes} min" if minutes else ""


def gradient_cover_path(title: str, portrait: bool = False) -> str:
    path = f"/api/image?text={quote((title or 'Media').strip() or 'Media')}&badge="
    return f"{path}&orientation=portrait" if portrait else path


def resolve_cover_url(value: str) -> str:
    value = str(value or "")
    idx = value.find("/api/image?")
    return f"{GRADIENT_COVER_BASE}{value[idx:]}" if idx != -1 else value


def format_tmdb_image(path: str, size="w500") -> str:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ""


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


def split_default_id(default_id) -> tuple:
    """Returns (imdb_id, tmdb_id, explicit_imdb, use_tmdb)."""
    if not default_id:
        return None, None, False, False
    value = str(default_id).strip()
    if value.startswith("tt"):
        return value, None, True, False
    if value.isdigit():
        return None, int(value), False, True
    return None, None, False, False


def empty_payload_base() -> dict:
    return {
        "tmdb_id": None,
        "imdb_id": None,
        "title": "",
        "year": 0,
        "rate": 0,
        "description": "",
        "poster": "",
        "backdrop": "",
        "logo": "",
        "cast": [],
        "runtime": "",
        "genres": [],
        "original_language": None,
        "origin_country": [],
    }



def ensure_media_ids(payload: dict, *, seed: str = "") -> dict:
    """Guarantee usable integer tmdb_id and string imdb_id for DB / Stremio / admin UI.

    - Real provider IDs are kept when present.
    - Missing tmdb_id → negative synthetic id (same pattern as manual/custom titles).
    - Missing imdb_id → ``tg{abs(tmdb_id)}`` so Stremio idPrefixes (tt|tg) still work.
    """
    if not isinstance(payload, dict):
        return payload

    tmdb = payload.get("tmdb_id")
    try:
        if tmdb is not None and str(tmdb).strip().lower() not in ("", "null", "none"):
            tmdb = int(tmdb)
        else:
            tmdb = None
    except (TypeError, ValueError):
        tmdb = None

    imdb = payload.get("imdb_id")
    if imdb is not None:
        imdb = str(imdb).strip()
        if imdb.lower() in ("", "null", "none"):
            imdb = None
        elif imdb.isdigit():
            imdb = f"tt{imdb}"
    else:
        imdb = None

    if tmdb is None:
        # Stable-ish synthetic id from available seeds so re-index merges
        import hashlib
        base = (
            seed
            or imdb
            or str(payload.get("kitsu_id") or "")
            or str(payload.get("tvdb_id") or "")
            or str(payload.get("title") or "")
            or "unknown"
        )
        digest = int(hashlib.md5(base.encode("utf-8")).hexdigest()[:8], 16)
        tmdb = -(digest % 1_000_000_000 + 1)

    if not imdb:
        imdb = f"tg{abs(int(tmdb))}"

    payload["tmdb_id"] = int(tmdb)
    payload["imdb_id"] = imdb
    return payload


def coerce_int_id(value, default=None):
    """Parse query/path tmdb_id that may arrive as 'null' / '' / None."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "undefined"):
        return default
    try:
        return int(float(s))  # tolerate "123.0"
    except (TypeError, ValueError):
        return default


def logo_from_imdb(imdb_id: str | None) -> str:
    """Metahub clearlogo built from an IMDb id (used when providers lack logos)."""
    if not imdb_id:
        return ""
    iid = str(imdb_id).strip()
    if not iid.startswith("tt"):
        iid = f"tt{iid}" if iid.isdigit() else iid
    return f"https://images.metahub.space/logo/medium/{iid}/img"


def normalize_rating(value) -> float:
    """Clamp provider scores into a 0–10 star-style rating.

    TVDB ``score`` is often a popularity rank (hundreds/thousands) — those
    must not surface as 953.4-style ratings.
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    if v <= 10:
        return round(v, 1)
    if v <= 100:
        # percentage-style (e.g. 82.5 → 8.3)
        return round(v / 10.0, 1)
    # Popularity / rank scores — not a star rating
    return 0.0


def parse_year_range(start=None, end=None) -> tuple:
    """Return (start_year:int|0, end_year:int|None) from provider fields."""
    def _y(val):
        if val is None or val == "":
            return None
        if isinstance(val, int):
            return val if val > 0 else None
        m = re.search(r"(19|20)\d{2}", str(val))
        return int(m.group(0)) if m else None

    s = _y(start)
    e = _y(end)
    if s and e and e < s:
        s, e = e, s
    if s and e and e == s:
        e = None
    return s or 0, e
