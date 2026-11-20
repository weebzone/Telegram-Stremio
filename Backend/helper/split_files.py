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
def part_info(file_name: str) -> Optional[Tuple[str, int]]:
    """Stable split-part identity that never depends on the caption.

    A part is only recognised as ``<video extension>.<NNN>`` (e.g. ``.mkv.001``),
    using the declared video formats. The caption can omit the quality, repeat the
    wrong part number or be empty, so the file name is the only trustworthy source.
    """
    if not file_name:
        return None

    match = _find_split_match(file_name.strip())
    if not match:
        return None

    start, end, part_num, ext = match
    base = _normalize(file_name.strip()[:start] + "." + ext)
    # Drop the media extension from the base so ".MKV.001" and ".mkv.001" match.
    base = re.sub(rf"\.(?:{_VIDEO_EXTENSIONS}|{_ARCHIVE_EXTENSIONS})$", "", base)
    return base, part_num


#----- Group key for every part of the same file. Quality is intentionally absent:
#----- parts of one upload may parse different (or no) quality from their captions.
def split_group_key(channel, file_name: str) -> Optional[str]:
    info = part_info(file_name)
    if not info:
        return None
    return f"{channel}:{info[0]}"


#----- Trailing ".NNN" at the very end of a name (no media extension required)
_TRAILING_PART_NUM_RE = re.compile(r"\.(\d{2,3})\s*$")


def resolve_part_identity(file_name: str, caption: str = "") -> Optional[Tuple[str, int]]:
    """Part identity for real-world uploads.

    Telegram's file name often drops the media extension (``...vip.003``) while
    the caption still has it (``...vip.mkv.003``). Only ``<video ext>.<NNN>`` is
    accepted as the part marker, so:

    1. full form in the file name wins;
    2. otherwise the part number comes from the file name's trailing ``.NNN``
       (the trustworthy source) and the base from the caption.
    """
    fn = (file_name or "").strip()
    cap = (caption or "").strip()

    info = part_info(fn)
    if info:
        return info

    number_match = _TRAILING_PART_NUM_RE.search(fn)
    if not number_match:
        return None

    caption_info = part_info(cap)
    if caption_info:
        return caption_info[0], int(number_match.group(1))

    return None


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
    if not match:
        return filename

    start, end, _part_num, ext = match
    return (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])
