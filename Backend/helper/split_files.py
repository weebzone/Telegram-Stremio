import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg'
# The ONLY supported split-archive convention: a 3-digit (allow 2 for safety)
# numeric suffix appended after the original video filename + extension, e.g.
# "Movie.1080p.mkv.001", ".002", ".003". No other numeric naming is treated as
# a split part, so normal files like "Anime-12.mkv" are never misclassified.
_TRAILING_NUMERIC_PATTERN = re.compile(rf'(?i)\.({_VIDEO_EXTENSIONS})\.(\d{{2,3}})$')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')


def _normalize(base: str) -> str:
    return _NORMALIZE_RE.sub('.', base).strip('.').lower()


def _find_split_match(name: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    # A numeric suffix after a real video extension (".mkv.001"). This is the
    # only accepted split format (HJSplit/7z style); it is unambiguous and
    # cannot collide with episode/season numbering.
    m = _TRAILING_NUMERIC_PATTERN.search(name)
    if m:
        return m.start(), m.end(), int(m.group(2)), m.group(1)

    return None


def parse_split_info(filename: str) -> Optional[Tuple[str, int]]:
    if not filename:
        return None

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return None

    start, end, part_num, ext = match
    remainder = (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])
    return _normalize(remainder), part_num


_COMBINED_EPISODES_RE = re.compile(
    r"E(?:P|PISODE)?[\s._-]*0*(\d{1,4})[\s._-]*(?:-|–|~|\+|&|,|to)+[\s._-]*(?:E(?:P|PISODE)?[\s._-]*)?0*(\d{1,4})(?=\D|$)",
    re.IGNORECASE,
)
_COMBINED_SEASON_RE = re.compile(r"S(?:EASON)?[\s._-]*0*(\d{1,3})", re.IGNORECASE)
_COMBINED_KEYWORD_RE = re.compile(r"\bcombined\b", re.IGNORECASE)


def _combined_season(name: str) -> Optional[int]:
    match = _COMBINED_SEASON_RE.search(name)
    return int(match.group(1)) if match else None


# Detect a combined file: an episode range ("S01 E04-06") or a whole-season
# "Combined" file. Returns {season, start, end} (start/end None for whole season).
def parse_combined_episodes(filename: str) -> Optional[dict]:
    if not filename:
        return None

    match = _COMBINED_EPISODES_RE.search(filename)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if 1 <= start < end <= 99:
            return {"season": _combined_season(filename) or 1, "start": start, "end": end}

    if _COMBINED_KEYWORD_RE.search(filename):
        season = _combined_season(filename)
        if season is not None:
            return {"season": season, "start": None, "end": None}

    return None


def strip_part_suffix(filename: str) -> str:
    if not filename:
        return filename

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return filename

    start, end, _part_num, ext = match
    return (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])
