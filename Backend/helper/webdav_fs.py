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

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from Backend import db
from Backend.helper.nfo_generator import episode_nfo, movie_nfo, season_nfo, tvshow_nfo
from Backend.logger import LOGGER


#----- sanitize a single path segment for Windows / media-server friendliness
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


@dataclass
class VNode:
    """One virtual filesystem node."""
    path: str                          # absolute virtual path, no trailing slash (except root)
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = field(default_factory=time.time)
    content_type: str = "application/octet-stream"
    # for files:
    kind: str = ""                     # movie_video | movie_nfo | show_nfo | season_nfo | episode_video | episode_nfo | poster
    # stream payload
    stream_id: Optional[str] = None    # QualityDetail.id (encoded stream hash)
    stream_name: Optional[str] = None
    parts: Optional[List[dict]] = None
    # nfo body (generated on demand if empty)
    nfo_body: Optional[bytes] = None
    # references back to DB
    media_type: Optional[str] = None
    tmdb_id: Optional[int] = None
    db_index: Optional[int] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    children: Dict[str, "VNode"] = field(default_factory=dict)


class WebDAVFilesystem:
    """
    Builds and caches a virtual tree from all storage databases.
    Cache TTL defaults to 5 minutes.
    """

    def __init__(self, cache_ttl: int = 300):
        self.cache_ttl = cache_ttl
        self._root: Optional[VNode] = None
        self._built_at: float = 0.0
        self._building = False

    def invalidate(self) -> None:
        self._built_at = 0.0
        self._root = None

    async def ensure_tree(self) -> VNode:
        now = time.time()
        if self._root is not None and (now - self._built_at) < self.cache_ttl:
            return self._root
        if self._building:
            # another coroutine is building; wait briefly for it
            for _ in range(50):
                await _async_sleep(0.1)
                if self._root is not None and (time.time() - self._built_at) < self.cache_ttl:
                    return self._root
        self._building = True
        try:
            root = await self._build_tree()
            self._root = root
            self._built_at = time.time()
            return root
        finally:
            self._building = False

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
                # case-insensitive fallback
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
        LOGGER.info("[WebDAV] Building virtual filesystem tree…")
        root = VNode(path="/", name="", is_dir=True)
        movies_dir = VNode(path="/Movies", name="Movies", is_dir=True)
        shows_dir = VNode(path="/TV Shows", name="TV Shows", is_dir=True)
        root.children["Movies"] = movies_dir
        root.children["TV Shows"] = shows_dir

        movie_count = 0
        show_count = 0

        # walk every storage DB
        storage_keys = sorted(
            [k for k in db.dbs.keys() if k.startswith("storage_")],
            key=lambda k: int(k.split("_")[1]),
        )
        for db_key in storage_keys:
            storage = db.dbs[db_key]
            try:
                db_index = int(db_key.split("_")[1])
            except ValueError:
                continue

            #----- Movies
            try:
                cursor = storage["movie"].find({})
                async for doc in cursor:
                    doc = _oid_str(doc)
                    doc.setdefault("db_index", db_index)
                    folder = movie_folder_name(doc)
                    # avoid collisions
                    base_folder = folder
                    n = 2
                    while folder in movies_dir.children:
                        folder = f"{base_folder} [{n}]"
                        n += 1
                    folder_path = f"/Movies/{folder}"
                    fnode = VNode(path=folder_path, name=folder, is_dir=True,
                                  media_type="movie", tmdb_id=doc.get("tmdb_id"), db_index=db_index)
                    movies_dir.children[folder] = fnode

                    # NFO
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
                    q = pick_best_quality(qualities)
                    # also expose every quality as separate file
                    for qual in qualities:
                        video_node = self._movie_video_node(folder_path, folder, doc, qual)
                        if video_node and video_node.name not in fnode.children:
                            fnode.children[video_node.name] = video_node
                    if not qualities and q is None:
                        pass
                    movie_count += 1
            except Exception as e:
                LOGGER.warning("[WebDAV] movie scan failed on %s: %s", db_key, e)

            #----- TV Shows
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
                    snode = VNode(path=folder_path, name=folder, is_dir=True,
                                  media_type="tv", tmdb_id=doc.get("tmdb_id"), db_index=db_index)
                    shows_dir.children[folder] = snode

                    # tvshow.nfo
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

                        # season.nfo
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
                            # one episode NFO (shared across qualities)
                            ep_nfo_name = f"{show_short} S{sn:02d}E{en:02d} - {ep_title}.nfo"
                            ep_nfo_bytes = episode_nfo(doc, sn, ep).encode("utf-8")
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

        LOGGER.info("[WebDAV] Tree ready: %s movies, %s shows", movie_count, show_count)
        return root

    def _movie_video_node(self, folder_path: str, folder: str, doc: dict, qual: dict) -> Optional[VNode]:
        qlabel = safe_name(str(qual.get("quality") or "Unknown"), 20)
        raw_name = qual.get("name") or folder
        ext = quality_ext(raw_name)
        fname = f"{folder} - {qlabel}{ext}"
        size = parse_size_bytes(qual.get("size"), qual.get("parts"))
        return VNode(
            path=f"{folder_path}/{fname}",
            name=fname,
            is_dir=False,
            size=size or 1,
            content_type=_mime_for_ext(ext),
            kind="movie_video",
            stream_id=qual.get("id"),
            stream_name=raw_name,
            parts=qual.get("parts"),
            media_type="movie",
            tmdb_id=doc.get("tmdb_id"),
            db_index=doc.get("db_index"),
        )

    def _episode_video_node(
        self,
        season_path: str,
        show_short: str,
        sn: int,
        en: int,
        ep_title: str,
        doc: dict,
        ep: dict,
        qual: dict,
    ) -> Optional[VNode]:
        qlabel = safe_name(str(qual.get("quality") or "Unknown"), 20)
        raw_name = qual.get("name") or f"{show_short}.S{sn:02d}E{en:02d}"
        ext = quality_ext(raw_name)
        fname = f"{show_short} S{sn:02d}E{en:02d} - {ep_title} - {qlabel}{ext}"
        size = parse_size_bytes(qual.get("size"), qual.get("parts"))
        return VNode(
            path=f"{season_path}/{fname}",
            name=fname,
            is_dir=False,
            size=size or 1,
            content_type=_mime_for_ext(ext),
            kind="episode_video",
            stream_id=qual.get("id"),
            stream_name=raw_name,
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
    path = path.replace("\\", "/")
    # decode is caller's job; here just clean
    while "//" in path:
        path = path.replace("//", "/")
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _mime_for_ext(ext: str) -> str:
    return {
        ".mkv": "video/x-matroska",
        ".mp4": "video/mp4",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".webm": "video/webm",
        ".ts": "video/mp2t",
        ".nfo": "text/xml; charset=utf-8",
        ".jpg": "image/jpeg",
        ".png": "image/png",
    }.get(ext.lower(), "application/octet-stream")


def _oid_str(doc: dict) -> dict:
    from bson import ObjectId
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        else:
            out[k] = v
    return out


async def _async_sleep(sec: float) -> None:
    import asyncio
    await asyncio.sleep(sec)


# singleton used by routes
fs = WebDAVFilesystem(cache_ttl=300)
