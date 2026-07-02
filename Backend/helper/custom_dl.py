import asyncio
import random
import re
import secrets
import time
import traceback
from collections import deque
from typing import Dict, List, Optional, Tuple, Union

from fastapi import Request
from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

from Backend import db
from Backend.helper.exceptions import FileNotFound
from Backend.helper.pyro import get_file_ids
from Backend.logger import LOGGER
from Backend.pyrofork.bot import client_avg_mbps, client_dc_map, client_failures, multi_clients, work_loads

ACTIVE_STREAMS: Dict[str, Dict] = {}
RECENT_STREAMS = deque(maxlen=20)


#----- Telegram file byte streamer with prefetch, multi-client parallelism, and telemetry
class ByteStreamer:
    CHUNK_SIZE = 1024 * 1024
    CLEAN_INTERVAL = 30 * 60
    _instances: Dict[int, "ByteStreamer"] = {}

    def __init__(self, client: Client, client_index: int = -1):
        self.client = client
        self.client_index = client_index
        self._file_id_cache: Dict[int, FileId] = {}
        self._session_lock = asyncio.Lock()
        if client_index >= 0:
            ByteStreamer._instances[client_index] = self
        asyncio.create_task(self._clean_cache())
        asyncio.create_task(self._prewarm_sessions())

    async def _prewarm_sessions(self):
        common_dcs = [1, 2, 4, 5]
        test_mode = await self.client.storage.test_mode()
        current_dc = await self.client.storage.dc_id()
        for dc in common_dcs:
            if dc in self.client.media_sessions or dc == current_dc:
                continue
            try:
                auth_key = await Auth(self.client, dc, test_mode).create()
                session = Session(self.client, dc, auth_key, test_mode, is_media=True)
                session.no_updates = True
                session.timeout = 30
                session.sleep_threshold = 60
                await session.start()
                imported = False
                for _ in range(6):
                    try:
                        exported = await self.client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc))
                        await session.send(raw.functions.auth.ImportAuthorization(id=exported.id,bytes=exported.bytes,))
                        imported = True
                        break
                    except AuthBytesInvalid:
                        await asyncio.sleep(0.5)
                    except OSError:
                        await asyncio.sleep(1)
                    except Exception:
                        break
                if imported:
                    self.client.media_sessions[dc] = session
                else:
                    await session.stop()
            except Exception:
                continue

    #----- Fetch (and cache) Telegram FileId properties for a message
    async def get_file_properties(self, chat_id: int, message_id: int) -> FileId:
        if message_id not in self._file_id_cache:
            file_id = await get_file_ids(self.client, int(chat_id), int(message_id))
            if not file_id:
                LOGGER.warning("Message %s not found", message_id)
                raise FileNotFound
            self._file_id_cache[message_id] = file_id
        return self._file_id_cache[message_id]

    #----- Build a prefetching, range-aware streaming generator for a file
    async def prefetch_stream(
        self,
        file_id: FileId,
        client_index: int,
        offset: int,
        first_part_cut: int,
        last_part_cut: int,
        part_count: int,
        chunk_size: int,
        prefetch: int,
        parallelism: int,
        stream_id: Optional[str] = None,
        meta: Optional[dict] = None,
        request: Optional[Request] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        extra_clients: Optional[List] = None,
    ):
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

        media_session = await self._get_media_session(file_id)
        location_box: List[object] = [await self._get_location(file_id)]
        async def _make_refresh_fn(loc_b, streamer_ref, file_id_ref):
            async def _refresh() -> bool:
                if not chat_id or not message_id:
                    return False
                try:
                    streamer_ref._file_id_cache.pop(message_id, None)
                    fresh = await get_file_ids(streamer_ref.client, chat_id, message_id)
                    if fresh:
                        streamer_ref._file_id_cache[message_id] = fresh
                        loc_b[0] = await ByteStreamer._get_location(fresh)
                        return True
                except Exception as exc:
                    LOGGER.warning("Location refresh failed for msg_id=%s: %s", message_id, exc)
                return False
            return _refresh

        primary_refresh = await _make_refresh_fn(location_box, self, file_id)
        session_pool = [(client_index, media_session, location_box, primary_refresh)]

        if extra_clients:
            for ec_idx, ec_streamer, ec_file_id in extra_clients:
                try:
                    ec_session = await ec_streamer._get_media_session(ec_file_id)
                    ec_loc_box = [await ByteStreamer._get_location(ec_file_id)]
                    ec_refresh = await _make_refresh_fn(ec_loc_box, ec_streamer, ec_file_id)
                    session_pool.append((ec_idx, ec_session, ec_loc_box, ec_refresh))
                except Exception as e:
                    LOGGER.warning("Skipping extra client %s (session setup failed): %s", ec_idx, e)

        async def fetch_chunk_with_retries(seq_idx: int, off: int) -> Tuple[int, Optional[bytes]]:
            slot = seq_idx % len(session_pool)
            c_idx, c_session, c_loc_box, c_refresh = session_pool[slot]

            tries = 0
            flood_tries = 0
            while tries < 3 and flood_tries < 5 and not stop_event.is_set():
                try:
                    r = await asyncio.wait_for(
                        c_session.send(
                            raw.functions.upload.GetFile(
                                location=c_loc_box[0], offset=off, limit=chunk_size
                            )
                        ),
                        timeout=15.0,
                    )
                    chunk_bytes = getattr(r, "bytes", None) if r else None

                    if chunk_bytes == b"":
                        return seq_idx, None

                    return seq_idx, chunk_bytes

                except asyncio.TimeoutError:
                    tries += 1
                    client_failures[c_idx] = client_failures.get(c_idx, 0) + 1
                    await asyncio.sleep(min(0.5 * (2 ** (tries - 1)), 10.0))

                except Exception as e:
                    err_str = str(e)

                    if "FILE_REFERENCE" in err_str or "file_reference" in err_str.lower():
                        await c_refresh()

                    flood_m = re.search(r'wait of (\d+) second', err_str, re.IGNORECASE)
                    if flood_m:
                        required = float(flood_m.group(1))
                        jitter = random.uniform(0.5, 2.0)
                        wait = required + jitter
                        flood_tries += 1
                        await asyncio.sleep(wait)
                    else:
                        tries += 1
                        backoff = min(0.5 * (2 ** (tries - 1)), 10.0)
                        await asyncio.sleep(backoff)
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
                                await q.put((None, None))
                                return

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
            _disconnect_check_counter = 0

            try:
                while True:
                    _disconnect_check_counter += 1
                    if _disconnect_check_counter % 8 == 0:
                        try:
                            if request and await request.is_disconnected():
                                ACTIVE_STREAMS[stream_id]["status"] = "cancelled"
                                break
                        except Exception:
                            pass

                    try:
                        off_chunk = await asyncio.wait_for(q.get(), timeout=90.0)
                    except asyncio.TimeoutError:
                        LOGGER.error("Producer stall (90 s) for stream %s — aborting", stream_id)
                        ACTIVE_STREAMS[stream_id]["status"] = "error"
                        break

                    if off_chunk is None:
                        break

                    off, chunk = off_chunk
                    if off is None and chunk is None:
                        break

                    if part_count == 1:
                        out_chunk = chunk[first_part_cut:last_part_cut]
                    elif current_part_idx == 1:
                        out_chunk = chunk[first_part_cut:]
                    elif current_part_idx == part_count:
                        out_chunk = chunk[:last_part_cut]
                    else:
                        out_chunk = chunk

                    try:
                        chunk_len = len(out_chunk)
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

                    yield out_chunk

                    current_part_idx += 1

            except asyncio.CancelledError:
                if not producer_task.done():
                    producer_task.cancel()
                ACTIVE_STREAMS[stream_id]["status"] = "cancelled"
                raise
            except Exception as e:
                LOGGER.exception("Consumer error for stream %s: %s", stream_id, e)
                ACTIVE_STREAMS[stream_id]["status"] = "error"
                if not producer_task.done():
                    producer_task.cancel()
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

                    prev = client_avg_mbps.get(client_index, 0.0)
                    if prev == 0.0:
                        client_avg_mbps[client_index] = avg_mbps
                    else:
                        client_avg_mbps[client_index] = 0.5 * prev + 0.5 * avg_mbps
                    
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
                        await asyncio.sleep(0.5)
                    except OSError:
                        await asyncio.sleep(1)

            self.client.media_sessions[dc] = session
            return session

    @staticmethod
    async def _get_location(file_id: FileId) -> Union[
        raw.types.InputDocumentFileLocation,
    ]:
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


#----- Speed test helper (runs independently, on-demand per file)
TEST_CHUNK_SIZE = 100 * 1024 * 1024


#----- Download a fixed slice from one client and measure throughput
async def _speed_test_single_client(
    client: Client,
    client_index: int,
    chat_id: int,
    message_id: int,
    progress_callback=None,
) -> dict:
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
        streamer = ByteStreamer(client)
        file_id = await streamer.get_file_properties(chat_id, message_id)

        media_session = await streamer._get_media_session(file_id)
        location = await ByteStreamer._get_location(file_id)
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
        dl_start = time.perf_counter()
        last_progress_time = dl_start
        total_bytes = 0
        chunk_size = 512 * 1024  
        max_concurrent_chunks = 8
        queue = asyncio.Queue()
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
                        eof_reached = True
                    now = time.perf_counter()
                    if progress_callback and (now - last_progress_time) >= 1.0:
                        elapsed_so_far = now - dl_start
                        if elapsed_so_far > 0:
                            current_speed = (total_bytes / (1024 * 1024)) / elapsed_so_far
                            prog_res = dict(result)
                            prog_res["bytes_downloaded"] = total_bytes
                            prog_res["time_taken_sec"] = round(elapsed_so_far, 3)
                            prog_res["speed_mbps"] = round(current_speed, 3)
                            if asyncio.iscoroutinefunction(progress_callback):
                                asyncio.create_task(progress_callback(prog_res))
                            else:
                                progress_callback(prog_res)
                        last_progress_time = now

                except asyncio.TimeoutError:
                    LOGGER.debug(
                        "Speed-test chunk timeout client=%s offset=%s (skipping)",
                        client_index, offset,
                    )
                except Exception as e:
                    LOGGER.debug(
                        "Speed-test fetch error client=%s offset=%s: %s",
                        client_index, offset, e,
                    )
                    
                finally:
                    queue.task_done()
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


#----- Run the speed test across every connected client, fastest first
async def run_speed_test(chat_id: int, message_id: int) -> List[dict]:
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
