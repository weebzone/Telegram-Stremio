import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg'
_TRAILING_NUMERIC_PATTERN = re.compile(rf'(?i)\.({_VIDEO_EXTENSIONS})\.(\d{{2,3}})$')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')


#----- Collapse separators into dots for stable grouping keys
def _normalize(base: str) -> str:
    return _NORMALIZE_RE.sub('.', base).strip('.').lower()


#----- Locate a trailing ".ext.NN" split-part marker in a name
def _find_split_match(name: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    m = _TRAILING_NUMERIC_PATTERN.search(name)
    if m:
        return m.start(), m.end(), int(m.group(2)), m.group(1)
    return None


#----- Parse a split filename into (group_key, part_number), or None
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


#----- Extract a season number from a combined-episode filename
def _combined_season(name: str) -> Optional[int]:
    match = _COMBINED_SEASON_RE.search(name)
    return int(match.group(1)) if match else None


#----- Detect combined-episode ranges (e.g. E01-E04), or None
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


#----- Remove a trailing split-part suffix from a filename
def strip_part_suffix(filename: str) -> str:
    if not filename:
        return filename

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return filename

    start, end, _part_num, ext = match
    return (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])
