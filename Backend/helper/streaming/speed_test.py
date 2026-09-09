"""
Client speed-test helpers for Telegram media downloads.

Provides _speed_test_single_client and run_speed_test used by the admin API
to measure throughput and latency of each bot client against a sample file.
Results are sorted by measured speed so the fastest client can be preferred.
"""

import asyncio
import time
from typing import List

from pyrogram import Client, raw

from Backend.helper.streaming.custom_dl import ByteStreamer
from Backend.logger import LOGGER
from Backend.pyrofork.bot import client_dc_map, multi_clients

TEST_CHUNK_SIZE = 100 * 1024 * 1024


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
                        client_index,
                        offset,
                    )
                except Exception as e:
                    LOGGER.debug(
                        "Speed-test fetch error client=%s offset=%s: %s",
                        client_index,
                        offset,
                        e,
                    )

                finally:
                    queue.task_done()

        workers = [asyncio.create_task(fetch_chunk_worker()) for _ in range(max_concurrent_chunks)]

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
