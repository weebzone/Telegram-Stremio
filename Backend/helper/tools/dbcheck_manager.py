from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from pyrogram.errors import FloodWait

from Backend.logger import LOGGER
from Backend.helper.encrypt import decode_string
from Backend.helper.tools.utils import _now, _fmt_elapsed

DBCHECK_CONCURRENCY = 5
DBCHECK_BATCH_DELAY = 0.3
DBCHECK_PAGE_SIZE = 100


class DbCheckManager:
    def __init__(self) -> None:
        self._db = None
        self._task: Optional[asyncio.Task] = None
        self._cancel = False
        self._lock = asyncio.Lock()
        self.state: Dict[str, Any] = self._blank_state()

    @staticmethod
    def _blank_state() -> Dict[str, Any]:
        return {
            "status": "idle",   
            "checked": 0,
            "alive": 0,
            "dead": 0,
            "errors": 0,
            "purged": 0,
            "speed": 0,
            "dead_entries": [],   
            "started_at": 0.0,
            "finished_at": 0.0,
            "error": None,
        }

    def bind_db(self, db) -> None:
        self._db = db

    def get_status(self) -> Dict[str, Any]:
        s = self.state
        elapsed = 0.0
        if s["started_at"]:
            end = s["finished_at"] or _now()
            elapsed = max(0.0, end - s["started_at"])
        return {
            "status": s["status"],
            "is_running": s["status"] == "running",
            "checked": s["checked"],
            "alive": s["alive"],
            "dead": s["dead"],
            "errors": s["errors"],
            "purged": s["purged"],
            "speed": s["speed"],
            "dead_count": len(s["dead_entries"]),
            "dead_entries": list(s["dead_entries"]),
            "elapsed": _fmt_elapsed(elapsed),
            "elapsed_seconds": int(elapsed),
            "error": s["error"],
        }

    #----- ── Control ───────────────────────────────────────────────────────────────
    async def start(self, client) -> Dict[str, Any]:
        async with self._lock:
            if self.state["status"] == "running":
                return {"ok": False, "message": "A DB check is already running."}
            self.state = self._blank_state()
            self.state["status"] = "running"
            self.state["started_at"] = _now()
            self._cancel = False
            self._task = asyncio.create_task(self._run(client))
            return {"ok": True, "message": "DB check started.", "status": self.get_status()}

    async def cancel(self) -> Dict[str, Any]:
        if self.state["status"] != "running":
            return {"ok": False, "message": "No DB check is currently running."}
        self._cancel = True
        return {"ok": True, "message": "Stop requested — finishing the current batch."}

    #----- ── Single-message check ───────────────────────────────────────────────────
    async def _check_message(self, client, stream_hash: str):
        try:
            decoded = await decode_string(stream_hash)
            if isinstance(decoded, dict) and "parts" in decoded:
                parts = decoded.get("parts") or []
                if not parts:
                    return False
                for part in parts:
                    alive = await self._check_one(client, part.get("chat_id"), part.get("msg_id"))
                    if alive is None:
                        return None
                    if not alive:
                        return False
                return True
            return await self._check_one(client, decoded.get("chat_id"), decoded.get("msg_id"))
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await self._check_message(client, stream_hash)
        except Exception:
            return None

    async def _check_one(self, client, chat_id, msg_id):
        if chat_id is None or msg_id is None:
            return False
        try:
            chat_id = int(f"-100{chat_id}")
            msg_id = int(msg_id)
            msg = await client.get_messages(chat_id, msg_id)
            if msg is None or msg.empty:
                return False
            return True
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return await self._check_one(client, str(chat_id).replace("-100", ""), msg_id)
        except Exception:
            return None

    async def _process_batch(self, client, batch: List[str]):
        tasks = [self._check_message(client, h) for h in batch]
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def _record_results(self, batch: List[str], results) -> None:
        s = self.state
        for stream_hash, result in zip(batch, results):
            s["checked"] += 1
            if result is True:
                s["alive"] += 1
            elif result is False:
                s["dead"] += 1
                title = None
                try:
                    title = await self._db.get_title_by_stream_id(stream_hash)
                except Exception:
                    pass
                s["dead_entries"].append({"id": stream_hash, "title": title or "Unknown"})
            else:
                s["errors"] += 1
        elapsed = max(1, int(_now() - s["started_at"]))
        s["speed"] = s["checked"] // elapsed

    #----- ── Worker ──────────────────────────────────────────────────────────────────
    async def _run(self, client) -> None:
        db = self._db
        s = self.state
        try:
            for i in range(1, db.current_db_index + 1):
                storage = db.dbs.get(f"storage_{i}")
                if storage is None:
                    continue

                #----- Movies
                last_id = None
                while not self._cancel:
                    query = {"_id": {"$gt": last_id}} if last_id else {}
                    docs = await storage["movie"].find(query).sort("_id", 1) \
                        .limit(DBCHECK_PAGE_SIZE).to_list(length=DBCHECK_PAGE_SIZE)
                    if not docs:
                        break
                    for movie in docs:
                        last_id = movie["_id"]
                        stream_ids = [q.get("id") for q in movie.get("telegram", []) if q.get("id")]
                        for x in range(0, len(stream_ids), DBCHECK_CONCURRENCY):
                            if self._cancel:
                                break
                            batch = stream_ids[x:x + DBCHECK_CONCURRENCY]
                            results = await self._process_batch(client, batch)
                            await self._record_results(batch, results)
                            await asyncio.sleep(DBCHECK_BATCH_DELAY)

                #----- TV
                last_id = None
                while not self._cancel:
                    query = {"_id": {"$gt": last_id}} if last_id else {}
                    docs = await storage["tv"].find(query).sort("_id", 1) \
                        .limit(DBCHECK_PAGE_SIZE).to_list(length=DBCHECK_PAGE_SIZE)
                    if not docs:
                        break
                    for show in docs:
                        last_id = show["_id"]
                        stream_ids = []
                        for season in show.get("seasons", []):
                            for episode in season.get("episodes", []):
                                for q in episode.get("telegram", []):
                                    if q.get("id"):
                                        stream_ids.append(q["id"])
                        for x in range(0, len(stream_ids), DBCHECK_CONCURRENCY):
                            if self._cancel:
                                break
                            batch = stream_ids[x:x + DBCHECK_CONCURRENCY]
                            results = await self._process_batch(client, batch)
                            await self._record_results(batch, results)
                            await asyncio.sleep(DBCHECK_BATCH_DELAY)

            s["status"] = "cancelled" if self._cancel else "completed"
            s["finished_at"] = _now()
            LOGGER.info(f"[DbCheck] {s['status']} — checked {s['checked']}, dead {s['dead']}")
        except asyncio.CancelledError:
            s["status"] = "cancelled"
            s["finished_at"] = _now()
            raise
        except Exception as e:
            s["status"] = "error"
            s["error"] = str(e)
            s["finished_at"] = _now()
            LOGGER.error(f"[DbCheck] Error: {e}")

    #----- ── Purge ────────────────────────────────────────────────────────────────────
    async def purge(self, stream_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        #----- Delete the given dead stream entries (defaults to the last check's); returns count purged
        db = self._db
        if stream_ids is None:
            stream_ids = [d["id"] for d in self.state.get("dead_entries", [])]
        stream_ids = [h for h in stream_ids if h]
        if not stream_ids:
            return {"ok": False, "message": "No dead links to purge.", "purged": 0}

        purged = 0
        for x in range(0, len(stream_ids), DBCHECK_CONCURRENCY):
            batch = stream_ids[x:x + DBCHECK_CONCURRENCY]
            results = await asyncio.gather(
                *[db.delete_media_by_stream_id(h) for h in batch],
                return_exceptions=True,
            )
            purged += sum(1 for r in results if r is True)

        #----- Drop purged ids from the in-memory dead list
        purged_set = set(stream_ids)
        self.state["dead_entries"] = [
            d for d in self.state.get("dead_entries", []) if d["id"] not in purged_set
        ]
        self.state["purged"] = self.state.get("purged", 0) + purged
        return {"ok": True, "message": f"Purged {purged} dead entr{'y' if purged == 1 else 'ies'}.",
                "purged": purged}




dbcheck_manager = DbCheckManager()
