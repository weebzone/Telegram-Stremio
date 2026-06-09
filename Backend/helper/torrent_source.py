import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".webm", ".mov", ".flv", ".wmv", ".m4v")
MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"]+", re.IGNORECASE)


def get_readable_file_size(size_in_bytes):
    size_in_bytes = int(size_in_bytes) if str(size_in_bytes).isdigit() else 0
    if not size_in_bytes:
        return "0B"

    index, size_units = 0, ["B", "KB", "MB", "GB", "TB", "PB"]
    while size_in_bytes >= 1024 and index < len(size_units) - 1:
        size_in_bytes /= 1024
        index += 1

    return f"{size_in_bytes:.2f}{size_units[index]}" if index > 0 else f"{size_in_bytes:.0f}B"


@dataclass
class TorrentItem:
    info_hash: str
    display_name: str
    file_name: str
    size_bytes: int | None
    size_text: str
    file_idx: int | None
    sources: list[str]
    source_label: str
    is_private: bool = False

    @property
    def unique_id(self) -> str:
        idx = "" if self.file_idx is None else str(self.file_idx)
        return f"torrent:{self.info_hash}:{idx}:{hashlib.sha1(self.file_name.encode('utf-8', errors='ignore')).hexdigest()[:12]}"


class BencodeParser:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def parse(self, pos: int = 0):
        if pos >= len(self.data):
            raise ValueError("Unexpected end of bencode data")
        token = self.data[pos:pos + 1]
        if token == b"i":
            return self._parse_int(pos)
        if token == b"l":
            return self._parse_list(pos)
        if token == b"d":
            return self._parse_dict(pos)
        if token.isdigit():
            return self._parse_bytes(pos)
        raise ValueError(f"Invalid bencode token at {pos}")

    def _parse_int(self, pos: int):
        end = self.data.index(b"e", pos)
        return int(self.data[pos + 1:end]), end + 1

    def _parse_bytes(self, pos: int):
        colon = self.data.index(b":", pos)
        length = int(self.data[pos:colon])
        start = colon + 1
        end = start + length
        return self.data[start:end], end

    def _parse_list(self, pos: int):
        pos += 1
        out = []
        while self.data[pos:pos + 1] != b"e":
            item, pos = self.parse(pos)
            out.append(item)
        return out, pos + 1

    def _parse_dict(self, pos: int):
        pos += 1
        out = {}
        while self.data[pos:pos + 1] != b"e":
            key, pos = self._parse_bytes(pos)
            value_start = pos
            value, pos = self.parse(pos)
            out[key] = value
            if key == b"info":
                out[b"__info_raw__"] = self.data[value_start:pos]
        return out, pos + 1


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _magnet_info_hash(value: str) -> str | None:
    value = value.strip()
    if len(value) == 40 and re.fullmatch(r"[A-Fa-f0-9]{40}", value):
        return value.lower()
    try:
        padded = value.upper() + ("=" * ((8 - len(value) % 8) % 8))
        decoded = base64.b32decode(padded)
        if len(decoded) == 20:
            return decoded.hex()
    except Exception:
        return None
    return None


def extract_magnet_links(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0).rstrip(".,)") for m in MAGNET_RE.finditer(text)]


def parse_magnet(magnet: str, fallback_name: str | None = None) -> TorrentItem:
    parsed = urlparse(magnet)
    qs = parse_qs(parsed.query)
    xt_values = qs.get("xt") or []
    info_hash = None
    for xt in xt_values:
        if xt.lower().startswith("urn:btih:"):
            info_hash = _magnet_info_hash(xt.split(":")[-1])
            break
    if not info_hash:
        raise ValueError("Magnet link does not contain a valid btih info hash")

    dn = qs.get("dn", [fallback_name or ""])[0]
    display_name = unquote(dn).strip() or fallback_name or info_hash
    trackers = [unquote(t) for t in qs.get("tr", []) if t]

    return TorrentItem(
        info_hash=info_hash,
        display_name=display_name,
        file_name=display_name,
        size_bytes=None,
        size_text="Torrent",
        file_idx=None,
        sources=_tracker_sources(trackers),
        source_label="magnet",
        is_private=False,
    )


def parse_torrent(data: bytes) -> list[TorrentItem]:
    root, pos = BencodeParser(data).parse(0)
    if pos != len(data):
        raise ValueError("Trailing data after torrent bencode")
    if not isinstance(root, dict) or b"info" not in root or b"__info_raw__" not in root:
        raise ValueError("Torrent file has no info dictionary")

    info = root[b"info"]
    info_hash = hashlib.sha1(root[b"__info_raw__"]).hexdigest()
    trackers = _torrent_trackers(root)
    sources = _tracker_sources(trackers)
    root_name = _text(info.get(b"name") or info.get(b"name.utf-8") or "torrent")
    is_private = int(info.get(b"private", 0) or 0) == 1

    raw_files = info.get(b"files")
    items: list[TorrentItem] = []
    if isinstance(raw_files, list):
        for idx, file_info in enumerate(raw_files):
            if not isinstance(file_info, dict):
                continue
            length = int(file_info.get(b"length", 0) or 0)
            path_parts = file_info.get(b"path.utf-8") or file_info.get(b"path") or []
            file_path = "/".join(_text(part) for part in path_parts) or root_name
            file_name = PurePosixPath(file_path).name
            if not _is_video(file_name):
                continue
            items.append(
                TorrentItem(
                    info_hash=info_hash,
                    display_name=f"{root_name}/{file_path}",
                    file_name=file_name,
                    size_bytes=length,
                    size_text=get_readable_file_size(length),
                    file_idx=idx,
                    sources=sources,
                    source_label="torrent",
                    is_private=is_private,
                )
            )
    else:
        length = int(info.get(b"length", 0) or 0)
        file_name = root_name
        if _is_video(file_name):
            items.append(
                TorrentItem(
                    info_hash=info_hash,
                    display_name=file_name,
                    file_name=file_name,
                    size_bytes=length,
                    size_text=get_readable_file_size(length),
                    file_idx=0,
                    sources=sources,
                    source_label="torrent",
                    is_private=is_private,
                )
            )

    if not items:
        raise ValueError("Torrent has no supported video files")
    return items


def _is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTENSIONS)


def _torrent_trackers(root: dict) -> list[str]:
    trackers = []
    announce = root.get(b"announce")
    if announce:
        trackers.append(_text(announce))
    announce_list = root.get(b"announce-list")
    if isinstance(announce_list, list):
        for tier in announce_list:
            if isinstance(tier, list):
                trackers.extend(_text(item) for item in tier)
            else:
                trackers.append(_text(tier))
    seen = set()
    out = []
    for tracker in trackers:
        tracker = tracker.strip()
        if tracker and tracker not in seen:
            seen.add(tracker)
            out.append(tracker)
    return out


def _tracker_sources(trackers: list[str]) -> list[str]:
    out = []
    for tracker in trackers:
        if tracker.startswith("tracker:"):
            out.append(tracker)
        elif tracker:
            out.append(f"tracker:{tracker}")
    return out
