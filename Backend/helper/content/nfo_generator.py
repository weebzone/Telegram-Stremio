from __future__ import annotations
import html
import re
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape


def _e(value: Any) -> str:
    if value is None:
        return ""
    return escape(str(value).strip())


def _runtime_minutes(runtime: Optional[str]) -> Optional[int]:
    if not runtime:
        return None
    s = str(runtime).strip().lower()
    m = re.search(r"(\d+)\s*h", s)
    h = int(m.group(1)) if m else 0
    m2 = re.search(r"(\d+)\s*m", s)
    mins = int(m2.group(1)) if m2 else 0
    if h or mins:
        return h * 60 + mins
    if s.isdigit():
        return int(s)
    return None


def _unique_list(items: Optional[List[Any]]) -> List[str]:
    if not items:
        return []
    seen = set()
    out: List[str] = []
    for it in items:
        s = str(it).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def movie_nfo(doc: Dict[str, Any]) -> str:
    """Build a <movie> NFO for a movie document."""
    title = doc.get("title_english") or doc.get("title") or "Unknown"
    original = doc.get("original_title") or title
    year = doc.get("release_year") or ""
    plot = doc.get("description") or ""
    rating = doc.get("rating")
    imdb_id = doc.get("imdb_id") or ""
    tmdb_id = doc.get("tmdb_id")
    genres = _unique_list(doc.get("genres"))
    cast = _unique_list(doc.get("cast"))
    runtime = _runtime_minutes(doc.get("runtime"))
    poster = doc.get("poster") or ""
    backdrop = doc.get("backdrop") or ""
    countries = _unique_list(doc.get("production_countries") or doc.get("origin_country"))
    language = doc.get("original_language") or ""

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<movie>",
        f"  <title>{_e(title)}</title>",
        f"  <originaltitle>{_e(original)}</originaltitle>",
    ]
    if year:
        lines.append(f"  <year>{_e(year)}</year>")
        lines.append(f"  <premiered>{_e(year)}-01-01</premiered>")
    if plot:
        lines.append(f"  <plot>{_e(plot)}</plot>")
        lines.append(f"  <outline>{_e(plot)}</outline>")
    if rating is not None:
        try:
            r = float(rating)
            lines.append(f"  <rating>{r:.1f}</rating>")
            lines.append("  <ratings>")
            lines.append(f'    <rating name="themoviedb" max="10" default="true"><value>{r:.1f}</value><votes>0</votes></rating>')
            lines.append("  </ratings>")
        except (TypeError, ValueError):
            pass
    if runtime:
        lines.append(f"  <runtime>{runtime}</runtime>")
    if imdb_id:
        lines.append(f"  <id>{_e(imdb_id)}</id>")
        lines.append(f"  <imdbid>{_e(imdb_id)}</imdbid>")
        lines.append(f"  <uniqueid type=\"imdb\" default=\"true\">{_e(imdb_id)}</uniqueid>")
    if tmdb_id:
        lines.append(f"  <tmdbid>{_e(tmdb_id)}</tmdbid>")
        lines.append(f"  <uniqueid type=\"tmdb\">{_e(tmdb_id)}</uniqueid>")
    for g in genres:
        lines.append(f"  <genre>{_e(g)}</genre>")
    for c in countries:
        lines.append(f"  <country>{_e(c)}</country>")
    if language:
        lines.append(f"  <language>{_e(language)}</language>")
    for i, actor in enumerate(cast[:40]):
        lines.append("  <actor>")
        lines.append(f"    <name>{_e(actor)}</name>")
        lines.append(f"    <order>{i}</order>")
        lines.append("  </actor>")
    if poster:
        lines.append(f"  <thumb aspect=\"poster\">{_e(poster)}</thumb>")
    if backdrop:
        lines.append(f"  <fanart><thumb>{_e(backdrop)}</thumb></fanart>")
    lines.append("  <source>Telegram-Stremio WebDAV</source>")
    lines.append("</movie>")
    return "\n".join(lines) + "\n"


def tvshow_nfo(doc: Dict[str, Any]) -> str:
    """Build a <tvshow> NFO for a series document."""
    title = doc.get("title_english") or doc.get("title") or "Unknown"
    original = doc.get("original_title") or title
    year = doc.get("release_year") or ""
    year_end = doc.get("release_year_end") or ""
    plot = doc.get("description") or ""
    rating = doc.get("rating")
    imdb_id = doc.get("imdb_id") or ""
    tmdb_id = doc.get("tmdb_id")
    genres = _unique_list(doc.get("genres"))
    cast = _unique_list(doc.get("cast"))
    poster = doc.get("poster") or ""
    backdrop = doc.get("backdrop") or ""
    countries = _unique_list(doc.get("production_countries") or doc.get("origin_country"))
    language = doc.get("original_language") or ""

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<tvshow>",
        f"  <title>{_e(title)}</title>",
        f"  <originaltitle>{_e(original)}</originaltitle>",
        f"  <showtitle>{_e(title)}</showtitle>",
    ]
    if year:
        lines.append(f"  <year>{_e(year)}</year>")
        lines.append(f"  <premiered>{_e(year)}-01-01</premiered>")
    if year_end:
        lines.append(f"  <ended>{_e(year_end)}</ended>")
    if plot:
        lines.append(f"  <plot>{_e(plot)}</plot>")
        lines.append(f"  <outline>{_e(plot)}</outline>")
    if rating is not None:
        try:
            r = float(rating)
            lines.append(f"  <rating>{r:.1f}</rating>")
            lines.append("  <ratings>")
            lines.append(f'    <rating name="themoviedb" max="10" default="true"><value>{r:.1f}</value><votes>0</votes></rating>')
            lines.append("  </ratings>")
        except (TypeError, ValueError):
            pass
    if imdb_id:
        lines.append(f"  <id>{_e(imdb_id)}</id>")
        lines.append(f"  <imdbid>{_e(imdb_id)}</imdbid>")
        lines.append(f'  <uniqueid type="imdb" default="true">{_e(imdb_id)}</uniqueid>')
    if tmdb_id:
        lines.append(f"  <tmdbid>{_e(tmdb_id)}</tmdbid>")
        lines.append(f'  <uniqueid type="tmdb">{_e(tmdb_id)}</uniqueid>')
    for g in genres:
        lines.append(f"  <genre>{_e(g)}</genre>")
    for c in countries:
        lines.append(f"  <country>{_e(c)}</country>")
    if language:
        lines.append(f"  <language>{_e(language)}</language>")
    for i, actor in enumerate(cast[:40]):
        lines.append("  <actor>")
        lines.append(f"    <name>{_e(actor)}</name>")
        lines.append(f"    <order>{i}</order>")
        lines.append("  </actor>")
    if poster:
        lines.append(f'  <thumb aspect="poster">{_e(poster)}</thumb>')
    if backdrop:
        lines.append(f"  <fanart><thumb>{_e(backdrop)}</thumb></fanart>")
    lines.append("  <source>Telegram-Stremio WebDAV</source>")
    lines.append("</tvshow>")
    return "\n".join(lines) + "\n"


def episode_nfo(
    show_doc: Dict[str, Any],
    season_number: int,
    episode: Dict[str, Any],
) -> str:
    """Build an <episodedetails> NFO for one episode."""
    show_title = show_doc.get("title_english") or show_doc.get("title") or "Unknown"
    ep_title = episode.get("title") or f"Episode {episode.get('episode_number', 0)}"
    ep_num = int(episode.get("episode_number") or 0)
    plot = episode.get("overview") or ""
    aired = episode.get("released") or ""
    imdb_id = show_doc.get("imdb_id") or ""
    tmdb_id = show_doc.get("tmdb_id")
    backdrop = episode.get("episode_backdrop") or show_doc.get("backdrop") or ""

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<episodedetails>",
        f"  <title>{_e(ep_title)}</title>",
        f"  <showtitle>{_e(show_title)}</showtitle>",
        f"  <season>{season_number}</season>",
        f"  <episode>{ep_num}</episode>",
    ]
    if plot:
        lines.append(f"  <plot>{_e(plot)}</plot>")
    if aired:
        # keep as-is if already ISO-ish
        lines.append(f"  <aired>{_e(str(aired)[:10])}</aired>")
    if imdb_id:
        lines.append(f'  <uniqueid type="imdb">{_e(imdb_id)}</uniqueid>')
    if tmdb_id:
        lines.append(f'  <uniqueid type="tmdb">{_e(tmdb_id)}</uniqueid>')
    if backdrop:
        lines.append(f'  <thumb>{_e(backdrop)}</thumb>')
    lines.append("  <source>Telegram-Stremio WebDAV</source>")
    lines.append("</episodedetails>")
    return "\n".join(lines) + "\n"


def season_nfo(show_doc: Dict[str, Any], season_number: int) -> str:
    """Minimal season.nfo."""
    title = show_doc.get("title_english") or show_doc.get("title") or "Unknown"
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        "<season>",
        f"  <title>{_e(title)} Season {season_number}</title>",
        f"  <seasonnumber>{season_number}</seasonnumber>",
        f"  <showtitle>{_e(title)}</showtitle>",
        "</season>",
    ]
    return "\n".join(lines) + "\n"
