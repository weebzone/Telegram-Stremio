import asyncio
import time
import secrets
from collections import deque
from typing import Dict, List, Union, Optional, Tuple, AsyncGenerator
import traceback
from fastapi import Request
from pyrogram import Client, raw, utils
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Session, Auth
from Backend.logger import LOGGER
from Backend.helper.exceptions import FIleNotFound
from Backend.helper.pyro import get_file_ids
from Backend import db
from Backend.pyrofork.bot import work_loads, multi_clients, client_dc_map, client_failures, client_avg_mbps

ACTIVE_STREAMS: Dict[str, Dict] = {}
RECENT_STREAMS = deque(maxlen=3)


def get_adaptive_chunk_size(client_index: int) -> int:
    """Return the best chunk size (bytes) for this client based on recent speed.

    Speed tiers:
      < 5  MB/s  → 512 KB  (small chunks, faster first-byte on slow sessions)
      5-20 MB/s  →   1 MB  (default)
      20-60 MB/s →   2 MB  (fewer round-trips on fast sessions)
      > 60 MB/s  →   4 MB  (maximise throughput on very fast sessions)
    """
    speed = client_avg_mbps.get(client_index, 0.0)
    if speed >= 60:
        return 4 * 1024 * 1024
    if speed >= 20:
        return 2 * 1024 * 1024
    if speed >= 5:
        return 1 * 1024 * 1024
    # Unknown speed or < 5 MB/s → start conservative
    return 512 * 1024

class ByteStreamer:
    CHUNK_SIZE = 1024 * 1024  # 1 MB
    CLEAN_INTERVAL = 30 * 60  # 30 minutes
    _instances: Dict[int, "ByteStreamer"] = {}  # client_index → streamer (for fallback)

    def __init__(self, client: Client, client_index: int = -1):
        self.client = client
        self.client_index = client_index
        self._file_id_cache: Dict[int, FileId] = {}
        self._session_lock = asyncio.Lock()
        # Register this streamer so fallback logic can reuse it
        if client_index >= 0:
            ByteStreamer._instances[client_index] = self
        asyncio.create_task(self._clean_cache())
        asyncio.create_task(self._prewarm_sessions())

    async def _prewarm_sessions(self):
        common_dcs = [1, 2, 4, 5]  # Main Telegram DCs
        LOGGER.debug("Pre-warming media sessions for common DCs...")
        
        for dc in common_dcs:
            try:
                if dc in self.client.media_sessions:
                    LOGGER.debug(f"Media session for DC {dc} already exists, skipping")
                    continue

                test_mode = await self.client.storage.test_mode()
                current_dc = await self.client.storage.dc_id()
 
                if dc == current_dc:
                    continue
                
                auth_key = await Auth(self.client, dc, test_mode).create()
                session = Session(self.client, dc, auth_key, test_mode, is_media=True)
                session.no_updates = True
                session.timeout = 30
                session.sleep_threshold = 60
                
                await session.start()
                
                for attempt in range(6):
                    try:
                        exported = await self.client.invoke(
                            raw.functions.auth.ExportAuthorization(dc_id=dc)
                        )
                        await session.send(
                            raw.functions.auth.ImportAuthorization(
                                id=exported.id, bytes=exported.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        LOGGER.debug(f"AuthBytesInvalid during pre-warm for DC {dc}; retrying...")
                        await asyncio.sleep(0.5)
                    except OSError:
                        LOGGER.debug(f"OSError during pre-warm for DC {dc}; retrying...")
                        await asyncio.sleep(1)
                    except Exception as e:
                        LOGGER.debug(f"Error during pre-warm for DC {dc}: {e}")
                        break
                
                self.client.media_sessions[dc] = session
                LOGGER.debug(f"Pre-warmed media session for DC {dc}")
                
            except Exception as e:
                LOGGER.debug(f"Could not pre-warm DC {dc}: {e}")
                continue

    async def get_file_properties(self, chat_id: int, message_id: int) -> FileId:
        if message_id not in self._file_id_cache:
            file_id = await get_file_ids(self.client, int(chat_id), int(message_id))
            if not file_id:
                LOGGER.warning("Message %s not found", message_id)
                raise FIleNotFound
            self._file_id_cache[message_id] = file_id
        return self._file_id_cache[message_id]

    async def prefetch_stream(
        self,
        file_id,
        client_index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        prefetch: int,
        stream_id: str,
        meta: dict,
        parallelism: int = 1,
        request = None,
        client_pool: list = None,
    ) -> AsyncGenerator[bytes, None]:
        if not stream_id:
            stream_id = secrets.token_hex(8)

        now = time.time()
        registry_entry = {
            "stream_id": stream_id,
            "msg_id": getattr(file_id, "local_id", None) or None,
            "chat_id": getattr(file_id, "chat_id", None),
            "dc_id": file_id.dc_id,
            "client_index": client_index,
            "start_ts": now,
            "last_ts": now,
            "total_bytes": 0,
            "avg_mbps": 0.0,
            "instant_mbps": 0.0,
            "peak_mbps": 0.0,
            "recent_measurements": deque(maxlen=3),
            "status": "active",
            "part_count": part_count,
            "prefetch": prefetch,
            "meta": meta or {},
        }

        ACTIVE_STREAMS[stream_id] = registry_entry
        work_loads[client_index] += 1

        queue_maxsize = max(1, prefetch)
        q: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        stop_event = asyncio.Event()

        multi_clients = getattr(self, "_multi_clients", None)
        if multi_clients is None:
            from Backend.pyrofork.bot import multi_clients
            
        client_pool = client_pool or [client_index]
        
        active_pool = []
        pool_sessions = {}
        for c in client_pool:
            c_streamer = ByteStreamer._instances.get(c)
            if c_streamer is None and c in multi_clients:
                c_streamer = ByteStreamer(multi_clients[c], c)
            if c_streamer:
                try:
                    pool_sessions[c] = await c_streamer._get_media_session(file_id)
                    active_pool.append(c)
                except Exception as e:
                    LOGGER.error("Could not get session for bot %s: %s", c, e)
                    
        if not active_pool:
            active_pool = [client_index]
            pool_sessions[client_index] = await self._get_media_session(file_id)
            
        LOGGER.debug("Striping stream %s across clients: %s", stream_id, active_pool)

        location = await self._get_location(file_id)

        async def fetch_chunk_with_retries(seq_idx: int, off: int) -> Tuple[int, Optional[bytes]]:
            """Fetch one chunk with timeout, exponential back-off, and round-robin bot striping.
            """
            primary_client_idx = active_pool[seq_idx % len(active_pool)]
            primary_session = pool_sessions[primary_client_idx]
            
            tries = 0
            while tries < 3 and not stop_event.is_set():
                use_session = primary_session
                use_client_idx = primary_client_idx
                
                if tries >= 1 and len(active_pool) > 1:
                    def _score(idx):
                        return work_loads.get(idx, 0) + 3 * client_failures.get(idx, 0)
                    fallback_idx = min(
                        (i for i in active_pool if i != primary_client_idx),
                        key=_score,
                        default=None,
                    )
                    if fallback_idx is not None:
                        use_session = pool_sessions[fallback_idx]
                        use_client_idx = fallback_idx
                        LOGGER.debug(
                            "Chunk fallback: seq=%s try=%s primary=%s → fallback=%s",
                            seq_idx, tries, primary_client_idx, fallback_idx,
                        )

                # --- attempt the fetch with a hard timeout ---
                try:
                    r = await asyncio.wait_for(
                        use_session.send(
                            raw.functions.upload.GetFile(
                                location=location, offset=off, limit=chunk_size
                            )
                        ),
                        timeout=15.0,
                    )
                    chunk_bytes = getattr(r, "bytes", None) if r else None
                    
                    if chunk_bytes == b"":
                        return seq_idx, None

                    # If we succeeded via a fallback, mark primary as degraded
                    if use_client_idx != primary_client_idx:
                        client_failures[primary_client_idx] = client_failures.get(primary_client_idx, 0) + 1
                    return seq_idx, chunk_bytes

                except asyncio.TimeoutError:
                    tries += 1
                    client_failures[use_client_idx] = client_failures.get(use_client_idx, 0) + 1
                    LOGGER.warning(
                        "Chunk timeout seq=%s off=%s try=%s client=%s",
                        seq_idx, off, tries, use_client_idx,
                    )
                except Exception as e:
                    tries += 1
                    LOGGER.debug(
                        "Fetch chunk error seq=%s off=%s try=%s client=%s err=%s",
                        seq_idx, off, tries, use_client_idx, getattr(e, "args", e),
                    )

                # Exponential back-off: 0.5 s, 1 s, 2 s, 4 s, 8 s, 10 s (cap)
                await asyncio.sleep(min(0.5 * (2 ** (tries - 1)), 10.0))

            LOGGER.error(
                "Failed to fetch chunk seq=%s off=%s after 3 retries, client=%s",
                seq_idx, off, primary_client_idx,
            )
            return seq_idx, None

        async def producer():
            try:
                if part_count <= 0:
                    await q.put((None, None))
                    return

                next_to_schedule = 0
                scheduled_tasks = {}
                results_buffer = {}
                next_to_put = 0
                max_parallel = max(1, parallelism)

                initial = min(part_count, max_parallel)
                for i in range(initial):
                    seq = next_to_schedule
                    off = offset + seq * chunk_size
                    task = asyncio.create_task(fetch_chunk_with_retries(seq, off))
                    scheduled_tasks[seq] = task
                    next_to_schedule += 1

                while next_to_put < part_count:
                    if stop_event.is_set():
                        break

                    if not scheduled_tasks:
                        seq = next_to_schedule
                        off = offset + seq * chunk_size
                        task = asyncio.create_task(fetch_chunk_with_retries(seq, off))
                        scheduled_tasks[seq] = task
                        next_to_schedule += 1

                    done, _ = await asyncio.wait(scheduled_tasks.values(), return_when=asyncio.FIRST_COMPLETED)

                    for completed in done:
                        try:
                            completed_seq = None
                            for k, t in list(scheduled_tasks.items()):
                                if t is completed:
                                    completed_seq = k
                                    break

                            if completed_seq is None:
                                continue

                            seq_idx, chunk_bytes = completed.result()
                            scheduled_tasks.pop(completed_seq, None)

                            if chunk_bytes is None:
                                LOGGER.error("Chunk fetch returned empty for stream=%s seq=%s. Filling with zero bytes.", stream_id, seq_idx)
                                chunk_bytes = b"\x00" * chunk_size

                            results_buffer[seq_idx] = chunk_bytes

                            if next_to_schedule < part_count:
                                seq = next_to_schedule
                                off = offset + seq * chunk_size
                                task = asyncio.create_task(fetch_chunk_with_retries(seq, off))
                                scheduled_tasks[seq] = task
                                next_to_schedule += 1

                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            LOGGER.exception("Error processing completed fetch task: %s%s", e, traceback.format_exc())
                            await q.put((None, None))
                            return

                    while next_to_put in results_buffer:
                        chunk_bytes = results_buffer.pop(next_to_put)
                        await q.put((offset + next_to_put * chunk_size, chunk_bytes))
                        next_to_put += 1

                await q.put((None, None))

            except asyncio.CancelledError:
                LOGGER.debug("Producer cancelled for stream %s", stream_id)
                try:
                    await q.put((None, None))
                except Exception:
                    pass
                raise
            except Exception as e:
                LOGGER.exception("Producer unexpected error for stream %s: %s", stream_id, e)
                try:
                    await q.put((None, None))
                except Exception:
                    pass

        async def consumer_generator():
            producer_task = asyncio.create_task(producer())
            current_part_idx = 1

            try:
                while True:
                    try:
                        if request and await request.is_disconnected():
                            LOGGER.debug("Client disconnected for stream %s; cancelling stream", stream_id)
                            ACTIVE_STREAMS[stream_id]["status"] = "cancelled"
                            break
                    except Exception:
                        pass

                    off_chunk = await q.get()
                    if off_chunk is None:
                        break

                    off, chunk = off_chunk
                    if off is None and chunk is None:
                        break

                    try:
                        chunk_len = len(chunk)
                    except Exception:
                        chunk_len = 0

                    now_ts = time.time()
                    elapsed = now_ts - ACTIVE_STREAMS[stream_id]["last_ts"]
                    if elapsed <= 0:
                        elapsed = 1e-6

                    recent = ACTIVE_STREAMS[stream_id]["recent_measurements"]
                    recent.append((chunk_len, elapsed))

                    if len(recent) >= 2:
                        total_bytes = sum(b for b, _ in recent)
                        total_time = sum(t for _, t in recent)
                        instant_mbps = min((total_bytes / (1024 * 1024)) / max(total_time, 0.01), 1000.0)
                    else:
                        instant_mbps = 0.0

                    ACTIVE_STREAMS[stream_id]["total_bytes"] += chunk_len
                    ACTIVE_STREAMS[stream_id]["last_ts"] = now_ts

                    total_time = now_ts - ACTIVE_STREAMS[stream_id]["start_ts"]
                    if total_time <= 0:
                        total_time = 1e-6

                    ACTIVE_STREAMS[stream_id]["avg_mbps"] = (ACTIVE_STREAMS[stream_id]["total_bytes"] / (1024 * 1024)) / total_time
                    ACTIVE_STREAMS[stream_id]["instant_mbps"] = instant_mbps

                    if instant_mbps > ACTIVE_STREAMS[stream_id]["peak_mbps"]:
                        ACTIVE_STREAMS[stream_id]["peak_mbps"] = instant_mbps

                    if part_count == 1:
                        yield chunk[first_part_cut:last_part_cut]
                    elif current_part_idx == 1:
                        yield chunk[first_part_cut:]
                    elif current_part_idx == part_count:
                        yield chunk[:last_part_cut]
                    else:
                        yield chunk

                    current_part_idx += 1

            except asyncio.CancelledError:
                LOGGER.debug("Consumer cancelled for stream %s", stream_id)
                if not producer_task.done():
                    producer_task.cancel()
                ACTIVE_STREAMS[stream_id]["status"] = "cancelled"
                raise
            except Exception as e:
                LOGGER.exception("Consumer error for stream %s: %s", stream_id, e)
                ACTIVE_STREAMS[stream_id]["status"] = "error"
                if not producer_task.done():
                    producer_task.cancel()
                raise
            finally:
                if not producer_task.done():
                    try:
                        producer_task.cancel()
                        await asyncio.wait_for(producer_task, timeout=2.0)
                    except (Exception, asyncio.CancelledError):
                        pass

                try:
                    end_ts = time.time()
                    total_bytes = ACTIVE_STREAMS[stream_id]["total_bytes"]
                    start_ts = ACTIVE_STREAMS[stream_id]["start_ts"]
                    duration = end_ts - start_ts if end_ts > start_ts else 0.0
                    avg_mbps = (total_bytes / (1024 * 1024)) / (duration if duration > 0 else 1e-6)

                    entry = ACTIVE_STREAMS.get(stream_id, {})
                    entry.update({
                        "end_ts": end_ts,
                        "duration": duration,
                        "avg_mbps": avg_mbps,
                        "status": "finished" if entry.get("status") == "active" else entry.get("status", "finished"),
                        "parallelism": parallelism,
                    })

                    # --- Update rolling average speed for this client ---
                    prev = client_avg_mbps.get(client_index, 0.0)
                    if prev == 0.0:
                        client_avg_mbps[client_index] = avg_mbps
                    else:
                        # Exponential moving average: 30% new, 70% history
                        client_avg_mbps[client_index] = 0.7 * prev + 0.3 * avg_mbps
                    
                    # --- Log Analytics to DB ---
                    entry["chunk_size"] = chunk_size
                    asyncio.create_task(db.log_stream_stats(entry))

                    async def delayed_pop():
                        await asyncio.sleep(3)
                        try:
                            if stream_id in ACTIVE_STREAMS:
                                RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(stream_id))
                        except Exception:
                            pass
                    
                    asyncio.create_task(delayed_pop())
                finally:
                    try:
                        work_loads[client_index] -= 1
                    except Exception:
                        pass

                stop_event.set()

        return consumer_generator()

    async def _get_media_session(self, file_id: FileId) -> Session:
        dc = file_id.dc_id
        media_session = self.client.media_sessions.get(dc)

        if media_session:
            return media_session

        async with self._session_lock:
            media_session = self.client.media_sessions.get(dc)
            if media_session:
                return media_session

            test_mode = await self.client.storage.test_mode()
            current_dc = await self.client.storage.dc_id()

            if dc != current_dc:
                auth_key = await Auth(self.client, dc, test_mode).create()
            else:
                auth_key = await self.client.storage.auth_key()

            session = Session(self.client, dc, auth_key, test_mode, is_media=True)
            session.no_updates = True
            session.timeout = 30 
            session.sleep_threshold = 60 

            await session.start()

            if dc != current_dc:
                for _ in range(6):
                    try:
                        exported = await self.client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc))
                        await session.send(raw.functions.auth.ImportAuthorization(id=exported.id, bytes=exported.bytes))
                        break
                    except AuthBytesInvalid:
                        LOGGER.debug("AuthBytesInvalid during media session import; retrying...")
                        await asyncio.sleep(0.5)
                    except OSError:
                        LOGGER.debug("OSError during media session import; retrying...")
                        await asyncio.sleep(1)

            self.client.media_sessions[dc] = session
            LOGGER.debug("Created media session for DC %s", dc)
            return session

    @staticmethod
    async def _get_location(file_id: FileId) -> Union[
        raw.types.InputPhotoFileLocation,
        raw.types.InputDocumentFileLocation,
        raw.types.InputPeerPhotoFileLocation,
    ]:
        ftype = file_id.file_type

        if ftype == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(user_id=file_id.chat_id, access_hash=file_id.chat_access_hash)
            else:
                if file_id.chat_access_hash == 0:
                    peer = raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                else:
                    peer = raw.types.InputPeerChannel(channel_id=utils.get_channel_id(file_id.chat_id),
                                                    access_hash=file_id.chat_access_hash)

            return raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )

        if ftype == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )

        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )

    async def _clean_cache(self) -> None:
        while True:
            await asyncio.sleep(self.CLEAN_INTERVAL)
            self._file_id_cache.clear()
            LOGGER.debug("ByteStreamer: cleared file_id cache")


# ---------------------------------------------------------------------------
# Speed Test helper – runs independently, on-demand per file
# ---------------------------------------------------------------------------

TEST_CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB per test download


async def _speed_test_single_client(
    client: Client,
    client_index: int,
    chat_id: int,
    message_id: int,
    progress_callback=None,
) -> dict:
    """
    Benchmark one client: fetch a FRESH FileId (file reference is per-session),
    then measure ping (time-to-first-byte) and download throughput for
    TEST_CHUNK_SIZE bytes of the target file.
    """
    dc_id = client_dc_map.get(client_index, "?")
    result = {
        "client_index": client_index,
        "dc_id": dc_id,
        "ping_ms": None,
        "speed_mbps": None,
        "time_taken_sec": None,
        "bytes_downloaded": 0,
        "error": None,
    }
    try:
        # Each client MUST fetch its own FileId — file references are
        # per-session and will raise FILE_REFERENCE_EXPIRED if shared.
        streamer = ByteStreamer(client)
        file_id = await streamer.get_file_properties(chat_id, message_id)

        media_session = await streamer._get_media_session(file_id)
        location = await ByteStreamer._get_location(file_id)

        # --- Ping: time to first byte ---
        ping_start = time.perf_counter()
        tiny = await media_session.send(
            raw.functions.upload.GetFile(location=location, offset=0, limit=4096)
        )
        ping_end = time.perf_counter()
        ping_ms = (ping_end - ping_start) * 1000
        result["ping_ms"] = round(ping_ms, 2)

        if not getattr(tiny, "bytes", None):
            result["error"] = "No data on ping probe"
            return result

        # --- Download: TEST_CHUNK_SIZE bytes with concurrency ---
        dl_start = time.perf_counter()
        last_progress_time = dl_start
        total_bytes = 0
        
        chunk_size = 512 * 1024  # 512 KB per request
        max_concurrent_chunks = 8 # Telegram caps around 8-10 connections/requests
        
        queue = asyncio.Queue()
        # Seed the queue with offsets from 0 to TEST_CHUNK_SIZE
        target_offsets = list(range(0, TEST_CHUNK_SIZE, chunk_size))
        for off in target_offsets:
            queue.put_nowait(off)
            
        eof_reached = False
        
        async def fetch_chunk_worker():
            nonlocal total_bytes, last_progress_time, eof_reached
            while not queue.empty() and not eof_reached:
                offset = queue.get_nowait()
                fetch_size = min(chunk_size, TEST_CHUNK_SIZE - offset)
                
                try:
                    r = await asyncio.wait_for(
                        media_session.send(
                            raw.functions.upload.GetFile(
                                location=location, offset=offset, limit=fetch_size
                            )
                        ),
                        timeout=15.0,
                    )
                    chunk = getattr(r, "bytes", None)
                    if not chunk:
                        eof_reached = True
                        queue.task_done()
                        continue
                        
                    bytes_got = len(chunk)
                    total_bytes += bytes_got
                    
                    if bytes_got < fetch_size:
                        eof_reached = True  # Natural EOF
                        
                    # Fire progress callback roughly every 1 second
                    now = time.perf_counter()
                    if progress_callback and (now - last_progress_time) >= 1.0:
                        elapsed_so_far = now - dl_start
                        if elapsed_so_far > 0:
                            current_speed = (total_bytes / (1024 * 1024)) / elapsed_so_far
                            prog_res = dict(result)
                            prog_res["bytes_downloaded"] = total_bytes
                            prog_res["time_taken_sec"] = round(elapsed_so_far, 3)
                            prog_res["speed_mbps"] = round(current_speed, 3)
                            
                            # Fire and forget callback (create_task) since we're in a worker
                            if asyncio.iscoroutinefunction(progress_callback):
                                asyncio.create_task(progress_callback(prog_res))
                            else:
                                progress_callback(prog_res)
                        last_progress_time = now

                except asyncio.TimeoutError:
                    # Expected during a speed probe — Telegram throttled or DC is slow.
                    # Log at DEBUG so it doesn't pollute the production error log.
                    LOGGER.debug(
                        "Speed-test chunk timeout client=%s offset=%s (skipping)",
                        client_index, offset,
                    )
                except Exception as e:
                    # Other transient errors (FloodWait, network blip, etc.)
                    LOGGER.debug(
                        "Speed-test fetch error client=%s offset=%s: %s",
                        client_index, offset, e,
                    )
                    
                finally:
                    queue.task_done()

        # Spawn workers
        workers = [
            asyncio.create_task(fetch_chunk_worker())
            for _ in range(max_concurrent_chunks)
        ]
        
        await queue.join()
        for w in workers:
            w.cancel()

        dl_end = time.perf_counter()
        elapsed = dl_end - dl_start
        if elapsed <= 0:
            elapsed = 1e-6

        speed_mbps = (total_bytes / (1024 * 1024)) / elapsed
        result["bytes_downloaded"] = total_bytes
        result["time_taken_sec"] = round(elapsed, 3)
        result["speed_mbps"] = round(speed_mbps, 3)

    except Exception as exc:
        result["error"] = str(exc)
        LOGGER.warning("Speed test failed for client %s (DC %s): %s", client_index, dc_id, exc)

    return result


async def run_speed_test(chat_id: int, message_id: int) -> List[dict]:
    """
    Run a parallel speed test against all active bot clients for the file
    identified by (chat_id, message_id).

    Each client fetches its own fresh FileId to avoid FILE_REFERENCE_EXPIRED.
    Returns a list of per-client result dicts sorted by speed descending.
    """
    if not multi_clients:
        return [{"error": "No bot clients connected"}]

    tasks = [
        _speed_test_single_client(client, idx, chat_id, message_id)
        for idx, client in multi_clients.items()
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)
    results.sort(
        key=lambda r: r.get("speed_mbps") or -1,
        reverse=True,
    )
    return list(results)
