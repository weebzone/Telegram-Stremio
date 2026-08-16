"""
Virtual filesystem for WebDAV backed by Telegram-Stremio MongoDB.

Layout (stable paths):

  /
  ├── Movies/
  │   └── Title (Year)/
  │       ├── Title (Year) - 1080p.mkv
  │       ├── Title (Year).nfo
  │       └── poster.jpg
  └── TV Shows/
      └── Show Name (Year)/
          ├── tvshow.nfo
          └── Season 01/
              ├── season.nfo
              ├── Show Name S01E01 - Episode Title - 1080p.mkv
              └── Show Name S01E01 - Episode Title.nfo
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from Backend import db
from Backend.helper.nfo_generator import episode_nfo, movie_nfo, season_nfo, tvshow_nfo
from Backend.logger import LOGGER

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, max_len: int = 120) -> str:
    s = _INVALID.sub("", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "Unknown"
    return s[:max_len]


def movie_folder_name(doc: Dict[str, Any]) -> str:
    title = doc.get("title_english") or doc.get("title") or "Unknown"
    year = doc.get("release_year")
    base = safe_name(title)
    if year:
        return f"{base} ({year})"
    tmdb = doc.get("tmdb_id")
    if tmdb:
        return f"{base} {{tmdb-{tmdb}}}"
    return base


def show_folder_name(doc: Dict[str, Any]) -> str:
    return movie_folder_name(doc)


def quality_ext(name: str) -> str:
    n = (name or "").lower()
    for ext in (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts"):
        if n.endswith(ext):
            return ext
    return ".mkv"


def pick_best_quality(qualities: Optional[List[dict]]) -> Optional[dict]:
    if not qualities:
        return None
    order = {"2160p": 0, "4k": 0, "1440p": 1, "1080p": 2, "720p": 3, "480p": 4, "360p": 5}
    def key(q):
        ql = str(q.get("quality") or "").lower()
        return order.get(ql, 50)
    return sorted(qualities, key=key)[0]


def parse_size_bytes(size_str: Any, parts: Optional[List[dict]] = None) -> int:
    if parts:
        total = 0
        for p in parts:
            try:
                total += int(p.get("size_bytes") or 0)
            except (TypeError, ValueError):
                pass
        if total > 0:
            return total
    if isinstance(size_str, (int, float)):
        return int(size_str)
    if not size_str:
        return 0
    s = str(size_str).strip().upper().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KMGT]?B?)$", s)
    if not m:
        try:
            return int(float(s))
        except ValueError:
            return 0
    num = float(m.group(1))
    unit = m.group(2) or "B"
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(num * mult.get(unit, 1))


def _oid_str(doc: dict) -> dict:
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


@dataclass
class VNode:
    path: str
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = field(default_factory=time.time)
    content_type: str = "application/octet-stream"
    kind: str = ""
    stream_id: Optional[str] = None
    stream_name: Optional[str] = None
    parts: Optional[List[dict]] = None
    nfo_body: Optional[bytes] = None
    media_type: Optional[str] = None
    tmdb_id: Optional[int] = None
    db_index: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    children: Dict[str, "VNode"] = field(default_factory=dict)


class WebDAVFilesystem:
    """
    Builds and caches a virtual tree from all storage databases.
    Increased cache TTL for large libraries (50k+ files).
    """

    def __init__(self, cache_ttl: int = 21600):  # 6 hours instead of 5 minutes
        self.cache_ttl = cache_ttl
        self._root: Optional[VNode] = None
        self._built_at: float = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._built_at = 0.0
        self._root = None

    async def ensure_tree(self) -> VNode:
        now = time.time()
        if self._root is not None and (now - self._built_at) < self.cache_ttl:
            return self._root

        async with self._lock:
            # Double-check after acquiring lock
            now = time.time()
            if self._root is not None and (now - self._built_at) < self.cache_ttl:
                return self._root

            LOGGER.info("[WebDAV] Building virtual filesystem tree (this can take a while with large libraries)…")
            root = await self._build_tree()
            self._root = root
            self._built_at = time.time()
            LOGGER.info("[WebDAV] Tree build finished")
            return root

    async def resolve(self, path: str) -> Optional[VNode]:
        root = await self.ensure_tree()
        path = normalize_path(path)
        if path in ("", "/"):
            return root
        parts = [p for p in path.strip("/").split("/") if p]
        node = root
        for part in parts:
            if not node.is_dir:
                return None
            child = node.children.get(part)
            if child is None:
                lower = part.lower()
                child = next((c for n, c in node.children.items() if n.lower() == lower), None)
            if child is None:
                return None
            node = child
        return node

    async def list_dir(self, path: str) -> List[VNode]:
        node = await self.resolve(path)
        if node is None or not node.is_dir:
            return []
        return list(node.children.values())

    async def _build_tree(self) -> VNode:
        root = VNode(path="/", name="", is_dir=True)
        movies_dir = VNode(path="/Movies", name="Movies", is_dir=True)
        shows_dir = VNode(path="/TV Shows", name="TV Shows", is_dir=True)
        root.children["Movies"] = movies_dir
        root.children["TV Shows"] = shows_dir

        movie_count = 0
        show_count = 0

        storage_keys = sorted(
            [k for k in db.dbs.keys() if k.startswith("storage_")],
            key=lambda k: int(k.split("_")[1]) if k.split("_")[1].isdigit() else 0,
        )

        for db_key in storage_keys:
            storage = db.dbs[db_key]
            try:
                db_index = int(db_key.split("_")[1])
            except ValueError:
                continue

            # Movies
            try:
                cursor = storage["movie"].find({})
                async for doc in cursor:
                    doc = _oid_str(doc)
                    doc.setdefault("db_index", db_index)
                    folder = movie_folder_name(doc)
                    base_folder = folder
                    n = 2
                    while folder in movies_dir.children:
                        folder = f"{base_folder} [{n}]"
                        n += 1
                    folder_path = f"/Movies/{folder}"
                    fnode = VNode(
                        path=folder_path, name=folder, is_dir=True,
                        media_type="movie", tmdb_id=doc.get("tmdb_id"), db_index=db_index
                    )
                    movies_dir.children[folder] = fnode

                    nfo_name = f"{folder}.nfo"
                    nfo_bytes = movie_nfo(doc).encode("utf-8")
                    fnode.children[nfo_name] = VNode(
                        path=f"{folder_path}/{nfo_name}",
                        name=nfo_name,
                        is_dir=False,
                        size=len(nfo_bytes),
                        content_type="text/xml; charset=utf-8",
                        kind="movie_nfo",
                        nfo_body=nfo_bytes,
                        media_type="movie",
                        tmdb_id=doc.get("tmdb_id"),
                        db_index=db_index,
                    )

                    qualities = doc.get("telegram") or []
                    for qual in qualities:
                        video_node = self._movie_video_node(folder_path, folder, doc, qual)
                        if video_node and video_node.name not in fnode.children:
                            fnode.children[video_node.name] = video_node
                    movie_count += 1
            except Exception as e:
                LOGGER.warning("[WebDAV] movie scan failed on %s: %s", db_key, e)

            # TV Shows
            try:
                cursor = storage["tv"].find({})
                async for doc in cursor:
                    doc = _oid_str(doc)
                    doc.setdefault("db_index", db_index)
                    folder = show_folder_name(doc)
                    base_folder = folder
                    n = 2
                    while folder in shows_dir.children:
                        folder = f"{base_folder} [{n}]"
                        n += 1
                    folder_path = f"/TV Shows/{folder}"
                    snode = VNode(
                        path=folder_path, name=folder, is_dir=True,
                        media_type="tv", tmdb_id=doc.get("tmdb_id"), db_index=db_index
                    )
                    shows_dir.children[folder] = snode

                    nfo_bytes = tvshow_nfo(doc).encode("utf-8")
                    snode.children["tvshow.nfo"] = VNode(
                        path=f"{folder_path}/tvshow.nfo",
                        name="tvshow.nfo",
                        is_dir=False,
                        size=len(nfo_bytes),
                        content_type="text/xml; charset=utf-8",
                        kind="show_nfo",
                        nfo_body=nfo_bytes,
                        media_type="tv",
                        tmdb_id=doc.get("tmdb_id"),
                        db_index=db_index,
                    )

                    for season in doc.get("seasons") or []:
                        sn = int(season.get("season_number") or 0)
                        season_name = f"Season {sn:02d}"
                        season_path = f"{folder_path}/{season_name}"
                        season_node = VNode(
                            path=season_path,
                            name=season_name,
                            is_dir=True,
                            media_type="tv",
                            tmdb_id=doc.get("tmdb_id"),
                            db_index=db_index,
                            season_number=sn,
                        )
                        snode.children[season_name] = season_node

                        snfo = season_nfo(doc, sn).encode("utf-8")
                        season_node.children["season.nfo"] = VNode(
                            path=f"{season_path}/season.nfo",
                            name="season.nfo",
                            is_dir=False,
                            size=len(snfo),
                            content_type="text/xml; charset=utf-8",
                            kind="season_nfo",
                            nfo_body=snfo,
                            media_type="tv",
                            tmdb_id=doc.get("tmdb_id"),
                            db_index=db_index,
                            season_number=sn,
                        )

                        for ep in season.get("episodes") or []:
                            en = int(ep.get("episode_number") or 0)
                            ep_title = safe_name(ep.get("title") or f"Episode {en}", 80)
                            show_short = safe_name(doc.get("title_english") or doc.get("title") or "Show", 60)
                            qualities = ep.get("telegram") or []
                            for qual in qualities:
                                vnode = self._episode_video_node(
                                    season_path, show_short, sn, en, ep_title, doc, ep, qual
                                )
                                if vnode and vnode.name not in season_node.children:
                                    season_node.children[vnode.name] = vnode

                            ep_nfo_name = f"{show_short} S{sn:02d}E{en:02d} - {ep_title}.nfo"
                            ep_nfo_bytes = episode_nfo(doc, season, ep).encode("utf-8")
                            if ep_nfo_name not in season_node.children:
                                season_node.children[ep_nfo_name] = VNode(
                                    path=f"{season_path}/{ep_nfo_name}",
                                    name=ep_nfo_name,
                                    is_dir=False,
                                    size=len(ep_nfo_bytes),
                                    content_type="text/xml; charset=utf-8",
                                    kind="episode_nfo",
                                    nfo_body=ep_nfo_bytes,
                                    media_type="tv",
                                    tmdb_id=doc.get("tmdb_id"),
                                    db_index=db_index,
                                    season_number=sn,
                                    episode_number=en,
                                )
                    show_count += 1
            except Exception as e:
                LOGGER.warning("[WebDAV] tv scan failed on %s: %s", db_key, e)

        LOGGER.info("[WebDAV] Built tree: %d movies, %d shows", movie_count, show_count)
        return root

    def _movie_video_node(self, folder_path: str, folder: str, doc: dict, qual: dict) -> Optional[VNode]:
        qname = str(qual.get("quality") or "Unknown").strip()
        ext = quality_ext(qual.get("name") or "")
        name = f"{folder} - {qname}{ext}"
        size = parse_size_bytes(qual.get("size"), qual.get("parts"))
        return VNode(
            path=f"{folder_path}/{name}",
            name=name,
            is_dir=False,
            size=size,
            content_type="video/x-matroska" if ext == ".mkv" else "video/mp4",
            kind="movie_video",
            stream_id=qual.get("id"),
            stream_name=qual.get("name"),
            parts=qual.get("parts"),
            media_type="movie",
            tmdb_id=doc.get("tmdb_id"),
            db_index=doc.get("db_index"),
        )

    def _episode_video_node(
        self, season_path, show_short, sn, en, ep_title, doc, ep, qual
    ) -> Optional[VNode]:
        qname = str(qual.get("quality") or "Unknown").strip()
        ext = quality_ext(qual.get("name") or "")
        name = f"{show_short} S{sn:02d}E{en:02d} - {ep_title} - {qname}{ext}"
        size = parse_size_bytes(qual.get("size"), qual.get("parts"))
        return VNode(
            path=f"{season_path}/{name}",
            name=name,
            is_dir=False,
            size=size,
            content_type="video/x-matroska" if ext == ".mkv" else "video/mp4",
            kind="episode_video",
            stream_id=qual.get("id"),
            stream_name=qual.get("name"),
            parts=qual.get("parts"),
            media_type="tv",
            tmdb_id=doc.get("tmdb_id"),
            db_index=doc.get("db_index"),
            season_number=sn,
            episode_number=en,
        )


def normalize_path(path: str) -> str:
    if not path:
        return "/"
    path = "/" + path.strip("/")
    return path if path != "/" else "/"


fs = WebDAVFilesystem(cache_ttl=21600)  # 6 hours
