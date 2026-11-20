import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg'
_ARCHIVE_EXTENSIONS = r'zip'
_TRAILING_NUMERIC_PATTERN = re.compile(rf'(?i)\.({_VIDEO_EXTENSIONS}|{_ARCHIVE_EXTENSIONS})\.(\d{{2,3}})(?=$|\D)')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')
_ARCHIVE_SET = {'zip'}


#----- Return the archive extension ('zip') if the name is a split archive part, else None
def split_archive_ext(filename: str) -> Optional[str]:
    if not filename:
        return None
    match = _find_split_match(filename.strip())
    if match and match[3] and match[3].lower() in _ARCHIVE_SET:
        return match[3].lower()
    return None


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


#----- Split-part identity from a Telegram file name: (base_key, part_number)
_PART_SUFFIX_RE = re.compile(r"\.(\d{2,3})\s*$")


def part_info(file_name: str) -> Optional[Tuple[str, int]]:
    """Stable split-part identity that never depends on the caption.

    The caption can omit the quality, repeat the wrong part number or be empty,
    so grouping and part numbering must come from the file name Telegram stores.
    Some uploads also drop the video extension ("...vip.003"), so a bare trailing
    number is accepted and the base is normalized without the video extension.
    """
    if not file_name:
        return None

    name = file_name.strip()
    match = _find_split_match(name)
    if match:
        start, end, part_num, ext = match
    else:
        bare = _PART_SUFFIX_RE.search(name)
        if not bare:
            return None
        start, end, part_num, ext = bare.start(), bare.end(), int(bare.group(1)), None

    remainder = (name[:start] + "." + ext) if ext else name[:start]
    base = _normalize(remainder)
    # The same upload may name parts "file.mkv.001" and "file.001"; drop the media
    # extension from the base so both land on the same group.
    base = re.sub(rf"\.(?:{_VIDEO_EXTENSIONS}|{_ARCHIVE_EXTENSIONS})$", "", base)
    return base, part_num


#----- Group key for every part of the same file. Quality is intentionally absent:
#----- parts of one upload may parse different (or no) quality from their captions.
def split_group_key(channel, file_name: str) -> Optional[str]:
    info = part_info(file_name)
    if not info:
        return None
    return f"{channel}:{info[0]}"


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


#----- Stable grouping key for combined files (episode range/keyword removed)
def combined_name_key(filename: str) -> str:
    if not filename:
        return ""
    name = _COMBINED_EPISODES_RE.sub("", filename)
    name = _COMBINED_KEYWORD_RE.sub("", name)
    return re.sub(r"[\s._-]+", " ", name).strip().lower()


#----- Remove a trailing split-part suffix from a filename
def strip_part_suffix(filename: str) -> str:
    if not filename:
        return filename

    name = filename.strip()
    match = _find_split_match(name)
    if match:
        start, end, _part_num, ext = match
        return (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])

    # Uploads that drop the video extension ("...vip.003")
    bare = _PART_SUFFIX_RE.search(name)
    if bare:
        return name[:bare.start()]
    return filename
