import asyncio
import math
import mimetypes
import secrets
import time
from collections import deque
from typing import Dict
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response as PlainResponse
from fastapi.responses import StreamingResponse

from Backend import db
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.custom_dl import ACTIVE_STREAMS, RECENT_STREAMS, ByteStreamer
from Backend.helper.encrypt import decode_string
from Backend.helper.utils import track_usage
from Backend.helper.virtual_dl import resolve_virtual_parts, virtual_stream_generator
from Backend.logger import LOGGER
from Backend.pyrofork.bot import (
    USERBOT_CLIENT_INDEX,
    Userbot,
    client_dc_map,
    client_failures,
    multi_clients,
    work_loads,
)

router = APIRouter(tags=["Streaming"])

_streamer_by_client: Dict = {}
_rr_counter: int = 0

_title_cache: Dict[str, tuple] = {}
_TITLE_CACHE_TTL = 300


#----- Recursively convert non-JSON-native containers to serializable forms
def make_json_safe(obj):
    if isinstance(obj, deque):
        return list(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(v) for v in obj]
    return obj


#----- Parse an HTTP Range header into (start, end) bounds
def parse_range_header(range_header: str, file_size: int):
    if not range_header:
        return 0, file_size - 1
    try:
        value = range_header.replace("bytes=", "").strip()
        start_str, end_str = value.split("-")
        if start_str == "":
            length = int(end_str)
            start = file_size - length
            end = file_size - 1
        elif end_str == "":
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str)
    except Exception:
        raise HTTPException(status_code=416, detail="Invalid Range header", headers={"Content-Range": f"bytes */{file_size}"})
    if start < 0:
        start = 0
    if end >= file_size:
        end = file_size - 1
    if end < start:
        raise HTTPException(status_code=416, detail="Requested Range Not Satisfiable", headers={"Content-Range": f"bytes */{file_size}"})
    return start, end


#----- Pick the least-loaded client, preferring the target DC, round-robin on ties
def select_best_client(target_dc: int) -> int:
    global _rr_counter

    def _score(idx: int) -> int:
        return work_loads.get(idx, 0) + 3 * client_failures.get(idx, 0)

    if target_dc > 0:
        matching = [idx for idx, dc in client_dc_map.items() if dc == target_dc and idx in multi_clients]
    else:
        matching = []
    if not matching:
        matching = list(multi_clients.keys())
    if not matching:
        return 0
    min_score = min(_score(i) for i in matching)
    tied = sorted(i for i in matching if _score(i) == min_score)
    selected = tied[_rr_counter % len(tied)]
    _rr_counter = (_rr_counter + 1) % max(len(multi_clients), 1)
    return selected


#----- Periodically decay recorded client failure counters
async def decay_client_failures() -> None:
    while True:
        await asyncio.sleep(300)
        for k in list(client_failures):
            if client_failures.get(k, 0) > 0:
                client_failures[k] = max(0, client_failures[k] - 1)


#----- Parallelism/prefetch factor scaled by the number of clients
def get_parallel_prefetch(client_count: int) -> tuple[int, int]:
    value = min(max(math.ceil(client_count / 5), 1), 5)
    return value, value


#----- Reuse (or lazily create) the cached ByteStreamer for a client index
def _get_streamer(tg_client, index: int) -> ByteStreamer:
    if tg_client not in _streamer_by_client:
        _streamer_by_client[tg_client] = ByteStreamer(tg_client, index)
    return _streamer_by_client[tg_client]


#----- Resolve a stream title from the TTL cache, DB, or the decoded URL name
async def _lookup_title(stream_id_hash: str, decoded_name: str):
    if not stream_id_hash:
        return decoded_name
    now = time.time()
    cached = _title_cache.get(stream_id_hash)
    if cached and now < cached[1]:
        return cached[0] or decoded_name
    db_title = await db.get_title_by_stream_id(stream_id_hash)
    _title_cache[stream_id_hash] = (db_title, now + _TITLE_CACHE_TTL)
    return db_title or decoded_name


#----- Derive a display file name and mime type from file properties
def _resolve_filename_mime(file_id):
    file_name = file_id.file_name or f"{secrets.token_hex(4)}.bin"
    mime_type = file_id.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    if "." not in file_name and "/" in mime_type:
        file_name = f"{file_name}.{mime_type.split('/')[1]}"
    return file_name, mime_type


#----- Build the shared streaming response headers and status code
def _build_stream_headers(mime_type, file_name, req_length, range_header, start, end, file_size):
    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(req_length),
        "Cache-Control": "public, max-age=3600",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    status = 200
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        status = 206
    return headers, status


#----- Entry point: decode the id and dispatch to the matching streamer
@router.get("/dl/{token}/{id}/{name}")
@router.head("/dl/{token}/{id}/{name}")
async def stream_handler(request: Request, token: str, id: str, name: str, token_data: dict = Depends(verify_token)):
    decoded = await decode_string(id)

    if decoded.get("global"):
        return await global_media_streamer(
            request=request, chat_id=int(decoded["chat_id"]), msg_id=int(decoded["msg_id"]),
            token=token, token_data=token_data, stream_id_hash=id,
        )

    if "parts" in decoded:
        return await virtual_media_streamer(
            request=request, parts_payload=decoded["parts"],
            token=token, token_data=token_data, stream_id_hash=id,
        )

    msg_id = decoded.get("msg_id")
    if not msg_id:
        raise HTTPException(status_code=400, detail="Missing id")
    chat_id = int(f"-100{decoded['chat_id']}")
    return await media_streamer(
        request=request, chat_id=chat_id, msg_id=int(msg_id),
        token=token, token_data=token_data, stream_id_hash=id,
    )


#----- Stream a single Telegram file, with optional multi-client parallelism
async def media_streamer(request: Request, chat_id: int, msg_id: int, token: str, token_data: dict = None, stream_id_hash: str = None):
    index = select_best_client(0)
    tg_client = multi_clients[index]
    streamer: ByteStreamer = _get_streamer(tg_client, index)
    file_id = await streamer.get_file_properties(chat_id=chat_id, message_id=msg_id)
    file_size = file_id.file_size
    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1
    chunk_size = 1024 * 1024
    offset = start - (start % chunk_size)
    first_part_cut = start - offset
    last_part_cut = (end % chunk_size) + 1
    part_count = math.ceil(end / chunk_size) - math.floor(offset / chunk_size)
    stream_id = secrets.token_hex(8)
    decoded_name = unquote(request.path_params.get("name", ""))
    final_title = await _lookup_title(stream_id_hash, decoded_name)
    meta = {
        "request_path": str(request.url.path),
        "client_host": request.client.host if request.client else None,
        "title": final_title,
        "user_name": token_data.get("name", "Unknown") if token_data else "Unknown"
    }

    token_count = len(multi_clients) - 1
    parallelism, prefetch_count = get_parallel_prefetch(token_count)
    extra_clients_for_stream = []
    if parallelism > 1 and len(multi_clients) > 1:
        other_indices = sorted((i for i in multi_clients if i != index), key=lambda i: work_loads.get(i, 0))

        async def _get_extra_file_id(ec_idx: int):
            ec_client = multi_clients[ec_idx]
            ec_streamer = _get_streamer(ec_client, ec_idx)
            try:
                ec_fid = await ec_streamer.get_file_properties(chat_id=chat_id, message_id=msg_id)
                return (ec_idx, ec_streamer, ec_fid)
            except Exception as e:
                LOGGER.warning("Extra client %s file_id fetch failed: %s", ec_idx, e)
                return None

        results = await asyncio.gather(*[_get_extra_file_id(i) for i in other_indices[:parallelism - 1]])
        extra_clients_for_stream = [r for r in results if r is not None]

    body_gen = await streamer.prefetch_stream(
        file_id=file_id,
        client_index=index,
        offset=offset,
        first_part_cut=first_part_cut,
        last_part_cut=last_part_cut,
        part_count=part_count,
        chunk_size=chunk_size,
        prefetch=prefetch_count,
        stream_id=stream_id,
        meta=meta,
        parallelism=parallelism,
        request=request,
        chat_id=chat_id,
        message_id=msg_id,
        extra_clients=extra_clients_for_stream,
    )

    asyncio.create_task(track_usage(stream_id, token, token_data))

    file_name, mime_type = _resolve_filename_mime(file_id)
    headers, status = _build_stream_headers(mime_type, file_name, req_length, range_header, start, end, file_size)

    if request.method == "HEAD":
        return PlainResponse(status_code=status, headers=headers)
    return StreamingResponse(body_gen, headers=headers, status_code=status, media_type=mime_type)


#----- Stream media reconstructed from multiple split parts
async def virtual_media_streamer(request: Request, parts_payload: list, token: str, token_data: dict = None, stream_id_hash: str = None):
    index = select_best_client(0)
    tg_client = multi_clients[index]
    streamer: ByteStreamer = _get_streamer(tg_client, index)

    parts, file_size = await resolve_virtual_parts(parts_payload, streamer)
    if not parts or file_size <= 0:
        raise HTTPException(status_code=404, detail="Split media parts not found")

    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1
    chunk_size = 1024 * 1024
    stream_id = secrets.token_hex(8)
    decoded_name = unquote(request.path_params.get("name", ""))
    final_title = await _lookup_title(stream_id_hash, decoded_name)

    meta = {
        "request_path": str(request.url.path),
        "client_host": request.client.host if request.client else None,
        "title": final_title,
        "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
        "split_parts": len(parts),
    }

    token_count = len(multi_clients) - 1
    parallelism, prefetch_count = get_parallel_prefetch(token_count)

    asyncio.create_task(track_usage(stream_id, token, token_data))

    file_name, mime_type = _resolve_filename_mime(parts[0]["file_id"])
    common_headers, status = _build_stream_headers(mime_type, file_name, req_length, range_header, start, end, file_size)

    if request.method == "HEAD":
        return PlainResponse(status_code=status, headers=common_headers)

    body_gen = virtual_stream_generator(
        parts=parts, start=start, end=end, chunk_size=chunk_size,
        streamer=streamer, client_index=index, request=request, meta=meta,
        stream_id=stream_id, parallelism=parallelism, prefetch_count=prefetch_count,
    )
    return StreamingResponse(body_gen, headers=common_headers, status_code=status, media_type=mime_type)


_userbot_streamer: ByteStreamer = None


#----- Lazily build and cache the ByteStreamer for the Userbot (None if unconfigured)
def _get_userbot_streamer() -> ByteStreamer:
    global _userbot_streamer
    if Userbot is None:
        return None
    if _userbot_streamer is None:
        _userbot_streamer = ByteStreamer(Userbot, USERBOT_CLIENT_INDEX)
    return _userbot_streamer


#----- Stream a Global Search file through the Userbot session directly
async def global_media_streamer(request: Request, chat_id: int, msg_id: int, token: str, token_data: dict = None, stream_id_hash: str = None):
    streamer = _get_userbot_streamer()
    if streamer is None:
        raise HTTPException(status_code=503, detail="Global Search streaming is unavailable (no Userbot configured)")

    LOGGER.info(f"[USERBOT] Stream request: chat={chat_id} msg={msg_id}")
    try:
        file_id = await streamer.get_file_properties(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        LOGGER.error(f"[USERBOT] File not accessible: chat={chat_id} msg={msg_id}: {e}")
        raise HTTPException(status_code=404, detail="File not accessible via Global Search")

    file_size = file_id.file_size
    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1
    chunk_size = 1024 * 1024
    offset = start - (start % chunk_size)
    first_part_cut = start - offset
    last_part_cut = (end % chunk_size) + 1
    part_count = math.ceil(end / chunk_size) - math.floor(offset / chunk_size)
    stream_id = secrets.token_hex(8)

    meta = {
        "request_path": str(request.url.path),
        "client_host": request.client.host if request.client else None,
        "title": file_id.file_name or "global-stream",
        "user_name": token_data.get("name", "Unknown") if token_data else "Unknown",
        "global_search": True,
    }

    asyncio.create_task(track_usage(stream_id, token, token_data))

    file_name, mime_type = _resolve_filename_mime(file_id)
    headers, status = _build_stream_headers(mime_type, file_name, req_length, range_header, start, end, file_size)

    if request.method == "HEAD":
        return PlainResponse(status_code=status, headers=headers)

    body_gen = await streamer.prefetch_stream(
        file_id=file_id,
        client_index=USERBOT_CLIENT_INDEX,
        offset=offset,
        first_part_cut=first_part_cut,
        last_part_cut=last_part_cut,
        part_count=part_count,
        chunk_size=chunk_size,
        prefetch=1,
        stream_id=stream_id,
        meta=meta,
        parallelism=1,
        request=request,
        chat_id=chat_id,
        message_id=msg_id,
    )
    return StreamingResponse(body_gen, headers=headers, status_code=status, media_type=mime_type)


#----- Live and recent stream telemetry, pruning stale active entries
@router.get("/stream/stats")
async def get_stream_stats():
    now = time.time()
    PRUNE_SECONDS = 3
    INACTIVE_TIMEOUT = 15
    for sid, info in list(ACTIVE_STREAMS.items()):
        status = info.get("status", "active")
        current_bytes = info.get("total_bytes", 0)
        if "last_bytes" not in info:
            info["last_bytes"] = current_bytes
            info["last_activity_ts"] = now
        if current_bytes > info["last_bytes"]:
            info["last_bytes"] = current_bytes
            info["last_activity_ts"] = now
            info["status"] = "active"
        else:
            if now - info["last_activity_ts"] > INACTIVE_TIMEOUT:
                if status == "active":
                    info["status"] = "cancelled"
                    info["end_ts"] = now
        if info.get("status") in ("cancelled", "error", "finished", "inactive"):
            last_ts = info.get("end_ts", info.get("last_activity_ts", now))
            if now - last_ts > PRUNE_SECONDS:
                try:
                    RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(sid))
                except KeyError:
                    pass

    active = [
        {
            "stream_id": sid,
            "msg_id": info.get("msg_id"),
            "chat_id": info.get("chat_id"),
            "title": info.get("meta", {}).get("title"),
            "client_index": info.get("client_index"),
            "dc_id": info.get("dc_id"),
            "status": info.get("status"),
            "total_bytes": info.get("total_bytes"),
            "instant_mbps": round(info.get("instant_mbps", 0.0), 3),
            "avg_mbps": round(info.get("avg_mbps", 0.0), 3),
            "peak_mbps": round(info.get("peak_mbps", 0.0), 3),
            "start_ts": info.get("start_ts"),
        }
        for sid, info in ACTIVE_STREAMS.items()
    ]
    recent = [
        {
            "stream_id": info.get("stream_id"),
            "msg_id": info.get("msg_id"),
            "chat_id": info.get("chat_id"),
            "title": info.get("meta", {}).get("title"),
            "client_index": info.get("client_index"),
            "dc_id": info.get("dc_id"),
            "status": info.get("status"),
            "total_bytes": info.get("total_bytes"),
            "duration": info.get("duration"),
            "avg_mbps": round(info.get("avg_mbps", 0.0), 3),
            "start_ts": info.get("start_ts"),
            "end_ts": info.get("end_ts"),
        }
        for info in RECENT_STREAMS
    ]
    return JSONResponse({
        "active_streams": active,
        "recent_streams": recent,
        "client_dc_map": client_dc_map,
        "work_loads": work_loads,
    })


#----- Detailed telemetry for a single stream id
@router.get("/stream/stats/{stream_id}")
async def get_stream_detail(stream_id: str):
    info = ACTIVE_STREAMS.get(stream_id)
    if info:
        return JSONResponse(make_json_safe(info))
    for rec in RECENT_STREAMS:
        if rec.get("stream_id") == stream_id:
            return JSONResponse(make_json_safe(rec))
    raise HTTPException(status_code=404, detail="Stream not found")
