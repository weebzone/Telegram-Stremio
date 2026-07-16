from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from pyrogram.errors import FloodWait, ChannelPrivate, ChatAdminRequired

from Backend.logger import LOGGER
from Backend.helper.encrypt import encode_string, decode_string
from Backend.helper.manual_add import stamp_caption_with_id
from Backend.helper.metadata import metadata
from Backend.helper.pyro import clean_filename, finalize_media_name, get_readable_file_size
from Backend.helper.split_files import parse_split_info
from Backend.helper.subtitles import ingest_subtitle, is_subtitle_file

SCAN_BATCH_SIZE = 200          
SCAN_MAX_EMPTY_BATCHES = 10    
SCAN_MAX_ID_CAP = 1_000_000    
SCAN_BATCH_DELAY = 0.5         
SCAN_PERSIST_EVERY = 1         
SCAN_PROBE_TEXT = "🔄"         
SCAN_PROCESS_CONCURRENCY = 8   

DBCHECK_CONCURRENCY = 5        
DBCHECK_BATCH_DELAY = 0.3      
DBCHECK_PAGE_SIZE = 100        

_STATE_COLLECTION = "scan_state"
_SCAN_DOC_ID = "scan"


def _now() -> float:
    return time.time()


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


class ScanManager:
    def __init__(self) -> None:
        self._db = None
        self._task: Optional[asyncio.Task] = None
        self._cancel = False
        self._lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self.state: Dict[str, Any] = self._blank_state()

    #----- ── State helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _blank_state() -> Dict[str, Any]:
        return {
            "status": "idle",            
            "mode": "scan",              
            "selected_channels": [],     
            "pending": [],               
            "current_channel": None,
            "current_channel_name": "",
            "current_id": 0,             
            "current_target_id": 0,      
            "cursors": {},               
            "counters": {
                "total_found": 0,
                "processed": 0,
                "indexed": 0,
                "skipped_dup": 0,
                "skipped_meta": 0,
                "skipped_nonvid": 0,
                "subtitles_added": 0,
                "subtitles_skipped": 0,
                "errors": 0,
            },
            "started_at": 0.0,
            "updated_at": 0.0,
            "finished_at": 0.0,
            "error": None,
        }

    @staticmethod
    def _blank_counters() -> Dict[str, int]:
        return {
            "total_found": 0,
            "processed": 0,
            "indexed": 0,
            "skipped_dup": 0,
            "skipped_meta": 0,
            "skipped_nonvid": 0,
            "subtitles_added": 0,
            "subtitles_skipped": 0,
            "errors": 0,
        }

    def bind_db(self, db) -> None:
        self._db = db

    async def load(self, db) -> None:
        self._db = db
        try:
            doc = await db.dbs["tracking"][_STATE_COLLECTION].find_one({"_id": _SCAN_DOC_ID})
        except Exception as e:
            LOGGER.error(f"[ScanManager] load failed: {e}")
            doc = None

        if doc:
            doc.pop("_id", None)
            merged = self._blank_state()
            merged.update(doc)
            merged["cursors"] = {str(k): int(v) for k, v in (merged.get("cursors") or {}).items()}
            if merged["status"] == "running":
                merged["status"] = "paused"
            self.state = merged
            if self.state["status"] == "paused":
                LOGGER.info("[ScanManager] Found an interrupted scan — marked as paused (resumable).")
            await self._persist()
        else:
            self.state = self._blank_state()

    async def _persist(self) -> None:
        if self._db is None:
            return
        self.state["updated_at"] = _now()
        try:
            doc = dict(self.state)
            doc["_id"] = _SCAN_DOC_ID
            await self._db.dbs["tracking"][_STATE_COLLECTION].update_one(
                {"_id": _SCAN_DOC_ID}, {"$set": doc}, upsert=True
            )
        except Exception as e:
            LOGGER.error(f"[ScanManager] persist failed: {e}")

    def get_status(self) -> Dict[str, Any]:
        s = self.state
        elapsed = 0.0
        if s["started_at"]:
            end = s["finished_at"] or _now()
            elapsed = max(0.0, end - s["started_at"])

        target = int(s.get("current_target_id", 0) or 0)
        cur = int(s.get("current_id", 0) or 0)
        progress = max(0, min(100, round(cur / target * 100))) if target > 0 else 0

        return {
            "status": s["status"],
            "mode": s["mode"],
            "is_running": s["status"] == "running",
            "resumable": s["status"] in ("paused", "cancelled") and bool(s["pending"]),
            "selected_channels": list(s["selected_channels"]),
            "pending": list(s["pending"]),
            "current_channel": s["current_channel"],
            "current_channel_name": s["current_channel_name"],
            "current_id": cur,
            "current_target_id": target,
            "progress": progress,
            "has_progress": target > 0,
            "counters": dict(s["counters"]),
            "elapsed": _fmt_elapsed(elapsed),
            "elapsed_seconds": int(elapsed),
            "error": s["error"],
        }

    async def _stream_id_exists(self, channel: int, msg_id: int) -> bool:
        db = self._db
        try:
            stream_hash = await encode_string({"chat_id": channel, "msg_id": msg_id})
        except Exception:
            stream_hash = None
        part_match = {"$elemMatch": {"chat_id": channel, "msg_id": msg_id}}
        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")
            if storage is None:
                continue
            if stream_hash:
                if await storage["movie"].find_one({"telegram.id": stream_hash}):
                    return True
                if await storage["tv"].find_one({"seasons.episodes.telegram.id": stream_hash}):
                    return True
            if await storage["movie"].find_one({"telegram.parts": part_match}):
                return True
            if await storage["tv"].find_one({"seasons.episodes.telegram.parts": part_match}):
                return True
        return False

    async def start(self, client, channels: List[str], mode: str = "scan") -> Dict[str, Any]:
        async with self._lock:
            if self.state["status"] == "running":
                return {"ok": False, "message": "A scan is already running."}

            channels = [str(c).strip() for c in (channels or []) if str(c).strip()]

            if mode == "scan" and not channels and self.state["pending"]:
                channels = list(self.state["pending"])

            if not channels:
                return {"ok": False, "message": "No channels selected."}

            if mode == "rescan":
                for ch in channels:
                    try:
                        ch_int = int(str(ch).replace("-100", ""))
                    except ValueError:
                        continue
                    try:
                        await self._purge_channel_entries(ch_int)
                    except Exception as e:
                        LOGGER.error(f"[ScanManager] purge failed for {ch}: {e}")
                    self.state["cursors"].pop(str(ch), None)
                self.state["selected_channels"] = list(channels)
                self.state["pending"] = list(channels)
                self.state["counters"] = self._blank_counters()
            else:
                resuming = self.state["status"] in ("paused", "cancelled") and self.state["pending"]
                if resuming:
                    merged_pending = list(self.state["pending"])
                    for ch in channels:
                        if ch not in merged_pending:
                            merged_pending.append(ch)
                    self.state["pending"] = merged_pending
                    self.state["selected_channels"] = list(
                        dict.fromkeys(self.state["selected_channels"] + channels)
                    )
                else:
                    self.state["selected_channels"] = list(channels)
                    self.state["pending"] = list(channels)
                    self.state["counters"] = self._blank_counters()

            self.state["mode"] = mode
            self.state["status"] = "running"
            self.state["error"] = None
            self.state["finished_at"] = 0.0
            self.state["started_at"] = _now()
            self._cancel = False
            await self._persist()

            self._task = asyncio.create_task(self._run(client))
            return {"ok": True, "message": f"{'Rescan' if mode == 'rescan' else 'Scan'} started.",
                    "status": self.get_status()}

    async def cancel(self) -> Dict[str, Any]:
        if self.state["status"] != "running":
            return {"ok": False, "message": "No scan is currently running."}
        self._cancel = True
        return {"ok": True, "message": "Stop requested — the scan will pause after the current batch."}

    async def _run(self, client) -> None:
        try:
            while self.state["pending"] and not self._cancel:
                ch = self.state["pending"][0]
                try:
                    ch_id = int(ch)
                except ValueError:
                    LOGGER.warning(f"[ScanManager] invalid channel id: {ch}")
                    self.state["pending"].pop(0)
                    await self._persist()
                    continue

                completed = await self._scan_channel(client, ch_id, ch)
                if self._cancel:
                    break
                if completed:
                    if self.state["pending"] and self.state["pending"][0] == ch:
                        self.state["pending"].pop(0)
                    await self._persist()

            if self._cancel:
                self.state["status"] = "cancelled"
                LOGGER.info("[ScanManager] Scan cancelled by user (resumable).")
            else:
                self.state["status"] = "completed"
                self.state["current_channel"] = None
                self.state["current_channel_name"] = ""
                LOGGER.info("[ScanManager] Scan completed.")
            self.state["finished_at"] = _now()
            await self._persist()

        except (ChannelPrivate, ChatAdminRequired) as e:
            self.state["status"] = "error"
            self.state["error"] = f"Access denied to channel — make sure the bot is an admin. ({e})"
            self.state["finished_at"] = _now()
            LOGGER.error(f"[ScanManager] {self.state['error']}")
            await self._persist()
        except asyncio.CancelledError:
            await self._persist()
            raise
        except Exception as e:
            self.state["status"] = "error"
            self.state["error"] = str(e)
            self.state["finished_at"] = _now()
            LOGGER.error(f"[ScanManager] Unexpected error: {e}")
            await self._persist()

    async def _scan_channel(self, client, chat_id: int, ch_key: str) -> bool:
        s = self.state

        try:
            chat = await client.get_chat(chat_id)
            s["current_channel_name"] = getattr(chat, "title", str(chat_id))
        except (ChannelPrivate, ChatAdminRequired):
            raise
        except Exception as e:
            s["current_channel_name"] = str(chat_id)
            LOGGER.warning(f"[ScanManager] Could not resolve channel name for {chat_id}: {e}")

        s["current_channel"] = ch_key

        last_id = await self._probe_last_message_id(client, chat_id)
        use_probe = last_id is not None and last_id >= 1
        s["current_target_id"] = last_id if use_probe else 0

        current = int(s["cursors"].get(str(ch_key), 1) or 1)
        LOGGER.info(
            f"[ScanManager] Scanning {s['current_channel_name']} ({chat_id}) from id {current}"
            + (f" up to {last_id} (probe)" if use_probe else " (heuristic mode — probe unavailable)")
        )

        empty_streak = 0
        batch_count = 0

        while not self._cancel and current < SCAN_MAX_ID_CAP:
            #----- ── Stop condition ───────────────────────────────────────────────
            if use_probe:
                if current > last_id:
                    break
            elif empty_streak >= SCAN_MAX_EMPTY_BATCHES:
                break

            upper = min(current + SCAN_BATCH_SIZE, SCAN_MAX_ID_CAP)
            if use_probe:
                upper = min(upper, last_id + 1)
            batch_ids = list(range(current, upper))
            if not batch_ids:
                break

            try:
                messages = await client.get_messages(chat_id, batch_ids)
            except FloodWait as e:
                LOGGER.info(f"[ScanManager] FloodWait {e.value}s — sleeping…")
                await asyncio.sleep(e.value)
                try:
                    messages = await client.get_messages(chat_id, batch_ids)
                except Exception as ex:
                    LOGGER.error(f"[ScanManager] Retry failed at {current}: {ex}")
                    s["counters"]["errors"] += 1
                    current = upper
                    empty_streak += 1
                    s["cursors"][str(ch_key)] = current
                    s["current_id"] = current
                    continue
            except Exception as e:
                LOGGER.error(f"[ScanManager] Batch fetch error at {current}: {e}")
                s["counters"]["errors"] += 1
                current = upper
                empty_streak += 1
                s["cursors"][str(ch_key)] = current
                s["current_id"] = current
                continue

            if not isinstance(messages, list):
                messages = [messages]

            to_process = [m for m in messages if m is not None and not m.empty]
            batch_had_content = bool(to_process)

            if to_process:
                s["counters"]["total_found"] += len(to_process)
                sem = asyncio.Semaphore(SCAN_PROCESS_CONCURRENCY)

                async def _worker(msg):
                    async with sem:
                        if self._cancel:
                            return
                        await self._process_message(msg, chat_id)
                        s["counters"]["processed"] += 1

                await asyncio.gather(*(_worker(m) for m in to_process))

            if self._cancel:
                s["cursors"][str(ch_key)] = current
                s["current_id"] = current
                await self._persist()
                return False

            empty_streak = 0 if batch_had_content else empty_streak + 1
            current = upper
            s["cursors"][str(ch_key)] = current
            s["current_id"] = current

            batch_count += 1
            if batch_count % SCAN_PERSIST_EVERY == 0:
                await self._persist()

            await asyncio.sleep(SCAN_BATCH_DELAY)

        await self._persist()
        LOGGER.info(f"[ScanManager] Finished {s['current_channel_name']} at id {current}")
        return True

    async def _probe_last_message_id(self, client, chat_id: int):
        probe = None
        try:
            probe = await client.send_message(chat_id, SCAN_PROBE_TEXT)
        except FloodWait as e:
            LOGGER.info(f"[ScanManager] FloodWait {e.value}s during probe — sleeping…")
            await asyncio.sleep(e.value)
            try:
                probe = await client.send_message(chat_id, SCAN_PROBE_TEXT)
            except Exception as ex:
                LOGGER.warning(f"[ScanManager] Probe send failed for {chat_id}: {ex}")
                return None
        except Exception as e:
            LOGGER.warning(f"[ScanManager] Could not send probe to {chat_id}: {e}")
            return None

        last_id = getattr(probe, "id", None)
        try:
            await client.delete_messages(chat_id, probe.id)
        except Exception as e:
            LOGGER.warning(
                f"[ScanManager] Could not delete probe message "
                f"{getattr(probe, 'id', None)} in {chat_id}: {e}"
            )
        return last_id

    async def _process_message(self, message, chat_id: int) -> None:
        s = self.state
        db = self._db

        #----- Subtitle files: match to a title and store, don't treat as media
        sub_name = message.document.file_name if message.document else ""
        if sub_name and is_subtitle_file(sub_name):
            channel_int = int(str(chat_id).replace("-100", ""))
            if await ingest_subtitle(sub_name, channel_int, message.id):
                s["counters"]["subtitles_added"] += 1
            else:
                s["counters"]["subtitles_skipped"] += 1
            return

        is_video = bool(message.video)
        is_supported = is_video
        if message.document and not is_video:
            mime = getattr(message.document, "mime_type", "") or ""
            if mime.startswith("video/"):
                is_supported = True
            else:
                candidate = message.caption or message.document.file_name or ""
                if parse_split_info(candidate):
                    is_supported = True

        if not is_supported:
            s["counters"]["skipped_nonvid"] += 1
            return

        file = message.video or message.document
        title = message.caption or file.file_name
        msg_id = message.id
        raw_size = file.file_size
        size = get_readable_file_size(file.file_size)
        channel_int = int(str(chat_id).replace("-100", ""))

        try:
            if await self._stream_id_exists(channel_int, msg_id):
                s["counters"]["skipped_dup"] += 1
                return
        except Exception as e:
            LOGGER.warning(f"[ScanManager] Dup-check error msg {msg_id}: {e}")

        try:
            metadata_info = await metadata(clean_filename(title), channel_int, msg_id)
        except Exception as e:
            LOGGER.warning(f"[ScanManager] Metadata exception for msg {msg_id}: {e}")
            metadata_info = None

        if metadata_info is None:
            s["counters"]["skipped_meta"] += 1
            return

        title_clean = finalize_media_name(title, bool(metadata_info.get('group_key')))

        try:
            async with self._db_lock:
                updated_id = await db.insert_media(
                    metadata_info,
                    channel=channel_int,
                    msg_id=msg_id,
                    size=size,
                    name=title_clean,
                    raw_size=raw_size,
                )
            if updated_id:
                s["counters"]["indexed"] += 1
                asyncio.create_task(stamp_caption_with_id(message, metadata_info))
            else:
                s["counters"]["skipped_meta"] += 1
        except Exception as e:
            LOGGER.error(f"[ScanManager] DB insert error msg {msg_id}: {e}")
            s["counters"]["errors"] += 1

    #----- ── Purge (rescan helper) ────────────────────────────────────────────────
    async def _purge_channel_entries(self, channel_int: int) -> int:
        db = self._db
        purged = 0
        try:
            await db.dbs["tracking"]["subtitles"].delete_many({"chat_id": channel_int})
        except Exception as e:
            LOGGER.warning(f"[ScanManager] subtitle purge failed for {channel_int}: {e}")
        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")
            if storage is None:
                continue

            async for movie in storage["movie"].find({}):
                remaining = []
                changed = False
                for q in movie.get("telegram", []):
                    try:
                        decoded = await decode_string(q["id"])
                        if int(decoded["chat_id"]) == channel_int:
                            purged += 1
                            changed = True
                            continue
                    except Exception:
                        pass
                    remaining.append(q)
                if changed:
                    if remaining:
                        movie["telegram"] = remaining
                        await storage["movie"].replace_one({"_id": movie["_id"]}, movie)
                    else:
                        await storage["movie"].delete_one({"_id": movie["_id"]})

            async for tv in storage["tv"].find({}):
                tv_changed = False
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        remaining = []
                        for q in episode.get("telegram", []):
                            try:
                                decoded = await decode_string(q["id"])
                                if int(decoded["chat_id"]) == channel_int:
                                    purged += 1
                                    tv_changed = True
                                    continue
                            except Exception:
                                pass
                            remaining.append(q)
                        episode["telegram"] = remaining
                    season["episodes"] = [ep for ep in season["episodes"] if ep.get("telegram")]
                tv["seasons"] = [se for se in tv["seasons"] if se.get("episodes")]
                if tv_changed:
                    if tv["seasons"]:
                        await storage["tv"].replace_one({"_id": tv["_id"]}, tv)
                    else:
                        await storage["tv"].delete_one({"_id": tv["_id"]})
        return purged


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


#----- ── Singletons ──────────────────────────────────────────────────────────────
scan_manager = ScanManager()
dbcheck_manager = DbCheckManager()
duplicate_manager = DuplicateManager()
