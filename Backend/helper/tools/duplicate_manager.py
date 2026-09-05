from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from Backend.logger import LOGGER
from Backend.helper.tools.utils import _now, _fmt_elapsed


class DuplicateManager:
    def __init__(self) -> None:
        self._db = None
        self._task: Optional[asyncio.Task] = None
        self._purge_task: Optional[asyncio.Task] = None
        self._cancel = False
        self._lock = asyncio.Lock()
        self.state: Dict[str, Any] = self._blank_state()

    @staticmethod
    def _blank_state() -> Dict[str, Any]:
        return {
            "status": "idle",
            "scanned": 0,
            "groups": [],
            "duplicate_count": 0,
            "purged": 0,
            "started_at": 0.0,
            "finished_at": 0.0,
            "error": None,
            "purge_status": "idle",
            "purge_total": 0,
            "purge_done": 0,
            "purge_started_at": 0.0,
            "purge_finished_at": 0.0,
        }

    def bind_db(self, db) -> None:
        self._db = db

    def get_status(self) -> Dict[str, Any]:
        s = self.state
        elapsed = 0.0
        if s["started_at"]:
            end = s["finished_at"] or _now()
            elapsed = max(0.0, end - s["started_at"])

        #----- Cleanup (purge) progress + ETA
        p_total = int(s.get("purge_total", 0) or 0)
        p_done = int(s.get("purge_done", 0) or 0)
        p_status = s.get("purge_status", "idle")
        p_elapsed = 0.0
        if s.get("purge_started_at"):
            p_end = s.get("purge_finished_at") or _now()
            p_elapsed = max(0.0, p_end - s["purge_started_at"])
        p_progress = round(p_done / p_total * 100) if p_total else 0
        p_eta = 0
        if p_status == "running" and p_done and p_elapsed > 0:
            rate = p_done / p_elapsed
            if rate > 0:
                p_eta = int(max(0, (p_total - p_done)) / rate)

        return {
            "status": s["status"],
            "is_running": s["status"] == "running",
            "scanned": s["scanned"],
            "group_count": len(s["groups"]),
            "duplicate_count": s["duplicate_count"],
            "purged": s["purged"],
            "groups": list(s["groups"]),
            "elapsed": _fmt_elapsed(elapsed),
            "elapsed_seconds": int(elapsed),
            "error": s["error"],
            "purge_status": p_status,
            "purge_running": p_status == "running",
            "purge_total": p_total,
            "purge_done": p_done,
            "purge_progress": p_progress,
            "purge_elapsed": _fmt_elapsed(p_elapsed),
            "purge_eta": _fmt_elapsed(p_eta) if p_eta else "—",
        }

    async def start(self) -> Dict[str, Any]:
        async with self._lock:
            if self.state["status"] == "running":
                return {"ok": False, "message": "A duplicate scan is already running."}
            if self.state.get("purge_status") == "running":
                return {"ok": False, "message": "A cleanup is currently running."}
            self.state = self._blank_state()
            self.state["status"] = "running"
            self.state["started_at"] = _now()
            self._cancel = False
            self._task = asyncio.create_task(self._run())
            return {"ok": True, "message": "Duplicate scan started.", "status": self.get_status()}

    async def cancel(self) -> Dict[str, Any]:
        if self.state["status"] != "running":
            return {"ok": False, "message": "No duplicate scan is currently running."}
        self._cancel = True
        return {"ok": True, "message": "Stop requested."}

    #----- Group a telegram list by (quality, name, size); record groups with 2+ entries
    def _collect(self, qualities: List[dict], label: str, media_type: str, gid: int) -> int:
        buckets: Dict[tuple, List[dict]] = {}
        for q in qualities:
            if not q.get("id"):
                continue
            buckets.setdefault(self._db._dup_key(q), []).append(q)
        for items in buckets.values():
            if len(items) < 2:
                continue
            gid += 1
            self.state["groups"].append({
                "group_id": gid,
                "title": label,
                "quality": items[0].get("quality"),
                "media_type": media_type,
                "entries": [
                    {"id": it["id"], "name": it.get("name"), "size": it.get("size")}
                    for it in items
                ],
            })
            self.state["duplicate_count"] += len(items) - 1
        return gid

    async def _run(self) -> None:
        db = self._db
        s = self.state
        try:
            gid = 0
            for i in range(1, db.current_db_index + 1):
                storage = db.dbs.get(f"storage_{i}")
                if storage is None:
                    continue

                async for movie in storage["movie"].find({}):
                    if self._cancel:
                        break
                    s["scanned"] += 1
                    year = movie.get("release_year")
                    label = f"{movie.get('title') or 'Unknown'}{f' ({year})' if year else ''}"
                    gid = self._collect(movie.get("telegram", []), label, "movie", gid)

                async for show in storage["tv"].find({}):
                    if self._cancel:
                        break
                    s["scanned"] += 1
                    title = show.get("title") or "Unknown"
                    for season in show.get("seasons", []):
                        for ep in season.get("episodes", []):
                            label = f"{title} S{season.get('season_number', 0):02d}E{ep.get('episode_number', 0):02d}"
                            gid = self._collect(ep.get("telegram", []), label, "tv", gid)

            s["status"] = "cancelled" if self._cancel else "completed"
            s["finished_at"] = _now()
            LOGGER.info(f"[Duplicates] {s['status']} — {len(s['groups'])} group(s), {s['duplicate_count']} redundant")
        except asyncio.CancelledError:
            s["status"] = "cancelled"
            s["finished_at"] = _now()
            raise
        except Exception as e:
            s["status"] = "error"
            s["error"] = str(e)
            s["finished_at"] = _now()
            LOGGER.error(f"[Duplicates] Error: {e}")

    #----- Delete duplicates: explicit ids, or (delete_all) keep the newest per group.
    #----- Runs in the background so the UI can poll deletion progress.
    async def purge(self, stream_ids: Optional[List[str]] = None, delete_all: bool = False) -> Dict[str, Any]:
        async with self._lock:
            if self.state.get("purge_status") == "running":
                return {"ok": False, "message": "A cleanup is already running."}

            ids: List[str] = []
            if delete_all:
                for g in self.state.get("groups", []):
                    ids.extend(e["id"] for e in g.get("entries", [])[:-1])
            elif stream_ids:
                ids = list(stream_ids)
            ids = [h for h in ids if h]
            if not ids:
                return {"ok": False, "message": "No duplicates selected to remove.", "purged": 0}

            self.state["purge_status"] = "running"
            self.state["purge_total"] = len(ids)
            self.state["purge_done"] = 0
            self.state["purge_started_at"] = _now()
            self.state["purge_finished_at"] = 0.0
            self._purge_task = asyncio.create_task(self._run_purge(ids))
            return {"ok": True, "message": f"Removing {len(ids)} duplicate(s)…",
                    "total": len(ids), "status": self.get_status()}

    async def _run_purge(self, ids: List[str]) -> None:
        db = self._db
        s = self.state
        purged = 0
        try:
            for h in ids:
                try:
                    if await db.delete_media_by_stream_id(h, delete_file=True):
                        purged += 1
                except Exception as e:
                    LOGGER.error(f"[Duplicates] purge failed for {h}: {e}")
                s["purge_done"] += 1

            purged_set = set(ids)
            new_groups = []
            for g in s.get("groups", []):
                remaining = [e for e in g.get("entries", []) if e["id"] not in purged_set]
                if len(remaining) >= 2:
                    g["entries"] = remaining
                    new_groups.append(g)
            s["groups"] = new_groups
            s["duplicate_count"] = sum(len(g["entries"]) - 1 for g in new_groups)
            s["purged"] = s.get("purged", 0) + purged
            s["purge_status"] = "completed"
            LOGGER.info(f"[Duplicates] cleanup completed — removed {purged}")
        except Exception as e:
            s["purge_status"] = "error"
            s["error"] = str(e)
            LOGGER.error(f"[Duplicates] cleanup error: {e}")
        finally:
            s["purge_finished_at"] = _now()




duplicate_manager = DuplicateManager()
