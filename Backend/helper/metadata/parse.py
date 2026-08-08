"""Filename / caption parsing helpers."""
from __future__ import annotations

import re
import traceback

import PTN
from guessit import guessit as _guessit

from Backend.helper.metadata.common import COMBINED_EPISODE_BASE, COMBINED_SEASON, first
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix
from Backend.logger import LOGGER

_MULTIPART_RE = re.compile(r"(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)", re.IGNORECASE)

# --- Explicit season/episode anchors -----------------------------------
# GuessIt will happily invent a season/episode by splitting any bare 3-4
# digit number in half (e.g. "One Piece - 1172" -> season 11, episode 72;
# or read a "(2011)" reboot-year tag as season 2011). PTN already catches
# real SxxExx / NxNN patterns on its own, so GuessIt's season/episode is
# only ever trusted when the filename has independent, explicit textual
# evidence for it (i.e. spelled-out "Season N"/"Series N" wording, which
# PTN does NOT parse by itself). Anything else falls through to the
# absolute-episode logic below instead of guessing.
_EXPLICIT_SXXEXX_RE = re.compile(r"(?i)\bs\d{1,2}[._\s-]*e\d{1,3}\b")
_EXPLICIT_NXNN_RE = re.compile(r"(?i)\b\d{1,2}x\d{2,3}\b")
_EXPLICIT_SEASON_WORD_RE = re.compile(r"(?i)\b(?:season|series)\s*0*\d{1,2}\b")


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
            parsed["title"] = parsed["title"] or first(g.get("title"))
            parsed["year"] = parsed["year"] or first(g.get("year"))

            # Only pull season/episode from GuessIt when PTN found neither
            # AND the filename has explicit textual evidence for a season
            # (spelled-out "Season"/"Series" wording, or a raw SxxExx/NxNN
            # PTN somehow missed). Never accept GuessIt's pure numeric
            # digit-splitting guess.
            if parsed["season"] is None and parsed["episode"] is None:
                has_anchor = bool(
                    _EXPLICIT_SXXEXX_RE.search(name)
                    or _EXPLICIT_NXNN_RE.search(name)
                    or _EXPLICIT_SEASON_WORD_RE.search(name)
                )
                if has_anchor:
                    g_season = first(g.get("season"))
                    if g_season is not None:
                        try:
                            g_season_int = int(g_season)
                        except (TypeError, ValueError):
                            g_season_int = None
                        if g_season_int is not None and g_season_int > 0:
                            parsed["season"] = g_season_int
                    parsed["episode"] = first(g.get("episode"))

            parsed["quality"] = parsed["quality"] or first(g.get("screen_size"))
        except Exception as e:
            LOGGER.warning(f"GuessIt parsing failed for {name}: {e}")

    # Normalize season 0 → None (specials folder only, not a real season for routing)
    try:
        if parsed.get("season") is not None and int(parsed["season"]) == 0:
            parsed["season"] = None
    except (TypeError, ValueError):
        pass

    return parsed


def apply_combined_override(payload: dict, combined: dict) -> None:
    season, start, end = combined["season"], combined["start"], combined["end"]
    payload["season_number"] = COMBINED_SEASON
    payload["episode_number"] = COMBINED_EPISODE_BASE + season
    payload["episode_title"] = f"Season {season} Combined"
    label = "Full" if start is None else f"E{start:02d}-E{end:02d}"
    payload["quality"] = f"{payload.get('quality') or 'HD'} {label}"
    if not payload.get("episode_backdrop"):
        payload["episode_backdrop"] = payload.get("backdrop") or payload.get("poster") or ""


def is_multipart_video(filename: str) -> bool:
    return bool(_MULTIPART_RE.search(filename or ""))



# Absolute / orphan episode patterns (no SxxExx), e.g. "One Piece 1223 720.mkv"
_SEASON_EP_RE = _EXPLICIT_SXXEXX_RE
# Resolution with an explicit trailing 'p' (unambiguous quality marker)
_RES_WITH_P_RE = re.compile(r"(?i)(?<![\w])(?:240|360|480|576|720|1080|1440|2160|4320)p(?![\w])")
# Bare resolution value with NO trailing 'p' (e.g. "One Piece 1223 720.mkv") is
# ambiguous with an absolute episode number that happens to equal a common
# resolution (episode 240, 480, 720...). Only treat it as quality when it's the
# trailing token right before the extension/end of string - conventionally
# where quality sits - and it's applied only as a fallback when no proper
# "NNNp" resolution was already found elsewhere in the name.
_RES_BARE_TRAILING_RE = re.compile(
    r"(?i)(?<![\w])(?:240|360|480|576|720|1080|1440|2160|4320)(?![\w])"
    r"(?=(?:[\s._-]*(?:\.[a-z0-9]{2,4})?)$)"
)
# Quality / codec / audio tokens
_QUALITY_TOKEN_RE = re.compile(
    r"(?i)(?:\d{3,4}x\d{3,4}|web-?dl|blu-?ray|bluray|hdtv|hdrip|webrip|bdrip|brrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|"
    r"(?:ddp|dd\+?|e?ac-?3|dts(?:-?hd)?|truehd|atmos)\s*\d?(?:[\s.]\d)?|"
    r"(?<!\d)\d[\s.]\d(?!\d)|10bit|8bit|"
    r"multi(?:\s*audio)?|dual(?:\s*audio)?|esub|subs?|softsubs?|hardsubs?|"
    r"(?<![\w])(?:bd|remux|encode)(?![\w]))"
)
# Bracketed release-group tags: [Judas], [SubsPlease], etc.
_RELEASE_GROUP_RE = re.compile(r"\[[^\]]{1,40}\]")
# Trailing absolute ep after title: "One Piece - 1172" / "One Piece 1172"
_TITLE_ABS_EP_RE = re.compile(
    r"(?i)^(?P<title>.+?)\s*[-–—]?\s*0*(?P<ep>\d{2,4})\s*$"
)
_YEAR_RE = re.compile(r"(?:^|[\s._\-(])((?:19|20)\d{2})(?:[\s._\-)]|$)")


def extract_absolute_episode(filename: str, parsed: dict | None = None) -> int | None:
    """Return absolute episode number when no season is present.

    Handles styles like:
      One Piece 1223 720.mkv
      One Piece - 1223 720p.mkv
      Naruto 500 1080p.mkv
      Naruto Shippuden - 016 480p BD x264 Multi Audio ESub
      [Judas] One Piece - 1172.mkv
    """
    parsed = parsed or {}
    try:
        if parsed.get("season") is not None and int(parsed.get("season")) > 0:
            return None
    except (TypeError, ValueError):
        if parsed.get("season") is not None:
            return None
    if _SEASON_EP_RE.search(filename or ""):
        return None

    ep = parsed.get("episode")
    if isinstance(ep, list):
        return None
    if ep is not None:
        try:
            return int(ep)
        except (TypeError, ValueError):
            pass

    name = filename or ""
    # Strip extension, release-group brackets, quality/codec tokens
    cleaned = re.sub(r"\.[a-z0-9]{2,4}$", " ", name, flags=re.I)
    cleaned = _RELEASE_GROUP_RE.sub(" ", cleaned)
    cleaned = _RES_WITH_P_RE.sub(" ", cleaned)
    # Only fall back to matching a bare (no-"p") resolution value when nothing
    # else in the name already claimed quality via an explicit "p" - otherwise
    # an absolute episode number that happens to equal 240/480/720/etc gets
    # eaten as if it were quality.
    if not _RES_WITH_P_RE.search(name):
        cleaned = _RES_BARE_TRAILING_RE.sub(" ", cleaned)
    cleaned = _QUALITY_TOKEN_RE.sub(" ", cleaned)
    # Strip years so 2021 is not treated as an episode
    cleaned = _YEAR_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\s._-]+", " ", cleaned).strip()

    # Explicit E/EP/Episode prefix wins
    prefixed = re.findall(r"(?i)(?:^|\s)(?:e|ep|episode)\s*0*(\d{1,4})(?:\s|$)", cleaned)
    if prefixed:
        return int(prefixed[-1])

    # Bare numbers left after stripping quality/year — prefer last 2–4 digit token
    # Include leading-zero forms like 016 (still 2–4 digit string length)
    bare = re.findall(r"(?:^|\s)(0*\d{1,4})(?:\s|$)", cleaned)
    # Filter pure years already stripped; drop 1-digit noise unless it's the only token
    candidates = []
    for x in bare:
        try:
            n = int(x)
        except ValueError:
            continue
        if n < 1:
            continue
        # Skip obvious non-episode leftovers (e.g. bitrate-like huge numbers already limited to 4 digits)
        candidates.append(n)
    if not candidates:
        return None
    # Prefer 3–4 digit (typical anime absolute); else last remaining (covers 016 → 16)
    long = [n for n in candidates if n >= 100]
    if long:
        return long[-1]
    return candidates[-1]


def clean_anime_search_title(title: str, absolute_ep: int | None = None) -> str:
    """Strip absolute episode / noise from a title used for provider search.

    "One Piece - 1172" + abs=1172 → "One Piece"
    "[Judas] One Piece" → "One Piece"
    """
    t = (title or "").strip()
    if not t:
        return t
    t = _RELEASE_GROUP_RE.sub(" ", t)
    t = re.sub(r"[\s._]+", " ", t).strip()
    if absolute_ep is not None:
        # Remove trailing " - 1172" / " 1172" / " E1172" matching this absolute
        t = re.sub(
            rf"(?i)\s*[-–—]?\s*(?:e|ep|episode)?\s*0*{int(absolute_ep)}\s*$",
            "",
            t,
        ).strip()
    # Generic trailing absolute-looking number (2–4 digits, optional E-prefix)
    # when no SxxExx
    if not _SEASON_EP_RE.search(t):
        t2 = re.sub(r"(?i)\s*[-–—]?\s*(?:e|ep|episode)?\s*0*\d{2,4}\s*$", "", t).strip()
        if t2:
            t = t2
    return t or (title or "").strip()


def is_absolute_episode(parsed: dict, filename: str = "") -> bool:
    """True when we have an episode number but no season (orphan/absolute style)."""
    try:
        if parsed.get("season") is not None and int(parsed.get("season")) > 0:
            return False
    except (TypeError, ValueError):
        if parsed.get("season") is not None:
            return False
    if _SEASON_EP_RE.search(filename or ""):
        return False
    if parsed.get("episode") is not None and not isinstance(parsed.get("episode"), list):
        return True
    return extract_absolute_episode(filename, parsed) is not None

def analyze_metadata_failure(filename: str) -> str:
    if is_multipart_video(filename or ""):
        return (
            "Looks like a multi-part video split (e.g. part1 / cd1) that can't be "
            "combined for streaming."
        )

    split_info = parse_split_info(filename or "")
    parse_target = strip_part_suffix(filename) if split_info else (filename or "")

    try:
        parsed = parse_media_name(parse_target)
    except Exception:
        return (
            "The file name / caption could not be parsed. Give it a clear name like "
            "'Movie Name (2021) 1080p'."
        )

    combined = parse_combined_episodes(parse_target)
    excess = parsed.get("excess")
    if not combined and excess and any("combined" in str(item).lower() for item in excess):
        return (
            "The caption says 'combined' but no season number could be read from it "
            "(e.g. name it 'Show S02 Combined')."
        )

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    quality = parsed.get("quality")

    if not combined and (isinstance(season, list) or isinstance(episode, list)):
        return (
            "The name spans multiple seasons (e.g. S01-S03) that can't be filed as one entry. "
            "Upload one season per file. Combined episode packs within a single season are fine "
            "when named like 'Show S02 E01-E05' or 'Show S02 Combined'."
        )
    if not quality:
        return (
            "No video quality/resolution was found. Add one to the caption "
            "(e.g. 480p, 720p, 1080p or 2160p)."
        )
    if not title:
        return "No title could be detected. Rename or caption the file with a clear title."

    return (
        "Could not match this title on the configured providers. Fix the title/year in the "
        "caption, or add an IMDb link/id (tt...) or a TMDB link/id, then forward it again."
    )
