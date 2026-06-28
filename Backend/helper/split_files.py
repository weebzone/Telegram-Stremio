import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv'
_TRAILING_NUMERIC_PATTERN = re.compile(rf'(?i)\.({_VIDEO_EXTENSIONS})\.(\d{{2,3}})$')
_NUMERIC_PATTERN = re.compile(r'(?i)[\.\-_](\d{2,3})(?=[\.\-_][a-z0-9]{2,4}$)')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')
# Tail of an episode range, e.g. the "E08" in "S01E05-E08" or "06" in "E01-06".
# Used to stop the weak numeric pattern from reading a range end as a split part.
_RANGE_TAIL_RE = re.compile(r'(?i)e?p?\d{1,4}$')


def _normalize(base: str) -> str:
    return _NORMALIZE_RE.sub('.', base).strip('.').lower()


def _find_split_match(name: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    # Strong signal: a numeric suffix after a real video extension (e.g. ".mkv.001").
    # This is the unambiguous HJSplit/7z style and is always treated as a split part.
    m = _TRAILING_NUMERIC_PATTERN.search(name)
    if m:
        return m.start(), m.end(), int(m.group(2)), m.group(1)

    # Weak signal: a bare number before the extension (e.g. "movie.001.dat").
    # Reject it when the number is actually the end of an episode range
    # ("Show.E01-06.mkv" -> "06" must NOT become split part 6).
    m = _NUMERIC_PATTERN.search(name)
    if m:
        part_num = int(m.group(1))
        separator = name[m.start()]
        prefix = name[:m.start()]
        is_range_tail = separator in '-–~' and bool(_RANGE_TAIL_RE.search(prefix))
        if not is_range_tail and 1 <= part_num <= 99:
            return m.start(), m.end(), part_num, None

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
    r"E(?:P|PISODE)?[\s._-]*0*(\d{1,4})[\s._-]*(?:-|–|~|to)+[\s._-]*(?:E(?:P|PISODE)?[\s._-]*)?0*(\d{1,4})(?=\D|$)",
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
