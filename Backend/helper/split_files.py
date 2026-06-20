import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv'
_TRAILING_NUMERIC_PATTERN = re.compile(rf'(?i)\.({_VIDEO_EXTENSIONS})\.(\d{{2,3}})$')
_NUMERIC_PATTERN = re.compile(r'(?i)[\.\-_](\d{2,3})(?=[\.\-_][a-z0-9]{2,4}$)')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')


def _normalize(base: str) -> str:
    return _NORMALIZE_RE.sub('.', base).strip('.').lower()


def _find_split_match(name: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    m = _TRAILING_NUMERIC_PATTERN.search(name)
    if m:
        return m.start(), m.end(), int(m.group(2)), m.group(1)

    m = _NUMERIC_PATTERN.search(name)
    if m:
        part_num = int(m.group(1))
        if 1 <= part_num <= 99:
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


def strip_part_suffix(filename: str) -> str:
    if not filename:
        return filename

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return filename

    start, end, _part_num, ext = match
    return (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])
