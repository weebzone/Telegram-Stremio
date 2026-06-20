import re
import time
from typing import Optional, Tuple

# Patterns tried in order. Each must capture the part number in its last group.
_NAMED_PATTERNS = [
    re.compile(r'(?i)[\.\-_ ](part)[\.\-_ ]?(\d{1,3})(?=[\.\-_ ]|$)'),
    re.compile(r'(?i)[\.\-_ ](cd)[\.\-_ ]?(\d{1,2})(?=[\.\-_ ]|$)'),
    re.compile(r'(?i)[\.\-_ ](dis[ck])[\.\-_ ]?(\d{1,2})(?=[\.\-_ ]|$)'),
]

# Bare numeric segment style: movie.001.mkv / movie-002.mp4
_NUMERIC_PATTERN = re.compile(r'(?i)[\.\-_](\d{2,3})(?=[\.\-_][a-z0-9]{2,4}$)')

_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')


def _normalize(base: str) -> str:
    base = _NORMALIZE_RE.sub('.', base).strip('.').lower()
    return base


def parse_split_info(filename: str) -> Optional[Tuple[str, int]]:
    if not filename:
        return None

    name = filename.strip()

    for pattern in _NAMED_PATTERNS:
        m = pattern.search(name)
        if m:
            try:
                part_num = int(m.group(2))
            except (ValueError, IndexError):
                continue
            remainder = name[:m.start()] + name[m.end():]
            return _normalize(remainder), part_num

    m = _NUMERIC_PATTERN.search(name)
    if m:
        part_num = int(m.group(1))
        # Numeric-only style is ambiguous (could be a year, resolution, etc.)
        # so require the number to plausibly be a part index, not a year.
        if 1 <= part_num <= 99:
            remainder = name[:m.start()] + name[m.end():]
            return _normalize(remainder), part_num

    return None


SPLIT_GROUP_WINDOW_SECONDS = 15 * 60  # 15 minutes


def fallback_group_key(filename: str, channel: int) -> str:
    """Group key to use when no explicit part marker is found, for matching
    against recently-seen identical filenames in the same channel."""
    return f"{channel}:{_normalize(filename)}"


def is_within_group_window(last_seen_ts: float, now_ts: Optional[float] = None) -> bool:
    now_ts = now_ts if now_ts is not None else time.time()
    return (now_ts - last_seen_ts) <= SPLIT_GROUP_WINDOW_SECONDS
