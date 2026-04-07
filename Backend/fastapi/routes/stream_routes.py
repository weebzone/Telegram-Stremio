import math
import secrets
import mimetypes
import time
from typing import Dict

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from collections import deque

from Backend import db
from Backend.helper.encrypt import decode_string
from Backend.helper.exceptions import InvalidHash
from Backend.helper.custom_dl import ByteStreamer, ACTIVE_STREAMS, RECENT_STREAMS, get_adaptive_chunk_size
from Backend.pyrofork.bot import StreamBot, work_loads, multi_clients, client_dc_map, client_failures, client_avg_mbps
from Backend.config import Telegram
from Backend.logger import LOGGER
from Backend.fastapi.security.tokens import verify_token
import asyncio

router = APIRouter(tags=["Streaming"])

_streamer_by_client: Dict = {}


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


def parse_range_header(range_header: str, file_size: int):
    """
    Parse HTTP Range header.

    Supports:
    bytes=1000-2000
    bytes=1000-
    bytes=-2000
    """
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
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if start < 0:
        start = 0

    if end >= file_size:
        end = file_size - 1

    if end < start:
        raise HTTPException(
            status_code=416,
            detail="Requested Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    return start, end


def select_best_client(target_dc: int) -> int:
    """Pick the best available client.

    Score = work_loads + 3 × client_failures
    Failures are weighted 3× so a bot that has been timing out / erroring
    is deprioritised even if its current workload is low.
    DC-aware selection is kept but currently commented out (uncomment to
    prefer same-DC bots).
    """
    def _score(idx: int) -> int:
        return work_loads.get(idx, 0) + 3 * client_failures.get(idx, 0)

    # --- DC-aware selection (Enabled) ---------------------------------------
    matching = [
        idx for idx, dc in client_dc_map.items()
        if dc == target_dc and idx in multi_clients
    ]
    if matching:
        selected = min(matching, key=_score)
        LOGGER.debug("DC-match client %s (DC %s) score=%s", selected, target_dc, _score(selected))
        return selected
    # ------------------------------------------------------------------------

    if multi_clients:
        selected = min(multi_clients.keys(), key=_score)
        LOGGER.debug(
            "Selected client %s (DC %s) score=%s",
            selected, client_dc_map.get(selected, "?"), _score(selected),
        )
        return selected

    return 0


async def decay_client_failures() -> None:
    """Every 5 minutes reduce each client's failure count by 1 (floor 0).

    This lets bots self-recover after a temporary DC issue without manual
    intervention.  The coroutine is started once as a background task on
    first import.
    """
    while True:
        await asyncio.sleep(300)  # 5 minutes
        for k in list(client_failures):
            if client_failures.get(k, 0) > 0:
                client_failures[k] = max(0, client_failures[k] - 1)
                LOGGER.debug("Failure decay: client %s failures → %s", k, client_failures[k])



async def track_usage_from_stats(stream_id: str, token: str, token_data: dict):
    await asyncio.sleep(2)
    
    limits = token_data.get("limits", {}) if token_data else {}
    usage = token_data.get("usage", {}) if token_data else {}
    
    daily_limit_gb = limits.get("daily_limit_gb")
    monthly_limit_gb = limits.get("monthly_limit_gb")
    
    initial_daily_bytes = usage.get("daily", {}).get("bytes", 0)
    initial_monthly_bytes = usage.get("monthly", {}).get("bytes", 0)
    
    last_tracked_bytes = 0
    update_interval = 10
    
    try:
        while True:
            await asyncio.sleep(update_interval)
            stream_info = ACTIVE_STREAMS.get(stream_id)
            if not stream_info:
                for rec in RECENT_STREAMS:
                    if rec.get("stream_id") == stream_id:
                        final_bytes = rec.get("total_bytes", 0)
                        delta = final_bytes - last_tracked_bytes
                        if delta > 0:
                            try:
                                await db.update_token_usage(token, delta)
                                LOGGER.debug(f"Final usage update for {stream_id}: {delta} bytes")
                            except Exception as e:
                                LOGGER.error(f"Final usage update failed: {e}")
                        break
                return
            
            current_bytes = stream_info.get("total_bytes", 0)
            delta = current_bytes - last_tracked_bytes
            
            if delta > 0:
                try:
                    await db.update_token_usage(token, delta)
                    last_tracked_bytes = current_bytes
                    LOGGER.debug(f"Updated usage for {stream_id}: +{delta} bytes (total: {current_bytes})")
                except Exception as e:
                    LOGGER.error(f"Periodic usage update failed: {e}")
            
            # Check limits (don't stop stream, just log - client manages connection)
            if daily_limit_gb and daily_limit_gb > 0:
                current_daily_gb = (initial_daily_bytes + current_bytes) / (1024 ** 3)
                if current_daily_gb >= daily_limit_gb:
                    LOGGER.debug(f"Daily limit reached for token, stream {stream_id} may be blocked by verify_token")
            
            if monthly_limit_gb and monthly_limit_gb > 0:
                current_monthly_gb = (initial_monthly_bytes + current_bytes) / (1024 ** 3)
                if current_monthly_gb >= monthly_limit_gb:
                    LOGGER.debug(f"Monthly limit reached for token, stream {stream_id} may be blocked by verify_token")
                    
    except asyncio.CancelledError:
        stream_info = ACTIVE_STREAMS.get(stream_id)
        if stream_info:
            current_bytes = stream_info.get("total_bytes", 0)
            delta = current_bytes - last_tracked_bytes
            if delta > 0:
                try:
                    await db.update_token_usage(token, delta)
                    LOGGER.info(f"Cancelled - final update for {stream_id}: {delta} bytes")
                except Exception as e:
                    LOGGER.error(f"Cancelled usage update failed: {e}")


@router.get("/dl/{token}/{id}/{name}")
@router.head("/dl/{token}/{id}/{name}")
async def stream_handler(
    request: Request,
    token: str,
    id: str,
    name: str,
    token_data: dict = Depends(verify_token),
):
    decoded = await decode_string(id)
    msg_id = decoded.get("msg_id")
    if not msg_id:
        raise HTTPException(status_code=400, detail="Missing id")

    chat_id = int(f"-100{decoded['chat_id']}")
    message = await StreamBot.get_messages(chat_id, int(msg_id))
    file = message.video or message.document
    secure_hash = file.file_unique_id[:6]

    return await media_streamer(
        request=request,
        chat_id=chat_id,
        msg_id=int(msg_id),
        secure_hash=secure_hash,
        token=token,
        token_data=token_data,
        stream_id_hash=id,
    )

async def media_streamer(
    request: Request,
    chat_id: int,
    msg_id: int,
    secure_hash: str,
    token: str,
    token_data: dict = None,
    stream_id_hash: str = None,
):
    temp_client = multi_clients[min(multi_clients.keys(), key=lambda i: work_loads.get(i, 0) + 3 * client_failures.get(i, 0))]
    if temp_client not in _streamer_by_client:
        idx = next((i for i, c in multi_clients.items() if c is temp_client), -1)
        _streamer_by_client[temp_client] = ByteStreamer(temp_client, idx)
    temp_streamer = _streamer_by_client[temp_client]

    file_id = await temp_streamer.get_file_properties(chat_id=chat_id, message_id=msg_id)

    if secure_hash != "SKIP_HASH_CHECK":  # Don't check this it is for my Webdav
        if file_id.unique_id[:6] != secure_hash:
            raise InvalidHash

    target_dc = file_id.dc_id
    LOGGER.debug(f"File msg_id={msg_id} is in DC {target_dc}")

    def _score(idx: int) -> int:
        from Backend.pyrofork.bot import work_loads, client_failures
        return work_loads.get(idx, 0) + 3 * client_failures.get(idx, 0)

    # Multi-connection striping: get all matching bots in DC, otherwise fallback globally
    dc_clients = [idx for idx, dc in client_dc_map.items() if dc == target_dc and idx in multi_clients]
    if dc_clients:
        client_pool = sorted(dc_clients, key=_score)
    else:
        client_pool = [select_best_client(target_dc)]

    # Primary client is the healthiest one
    index = client_pool[0]
    tg_client = multi_clients[index]

    if tg_client not in _streamer_by_client:
        _streamer_by_client[tg_client] = ByteStreamer(tg_client, index)
    streamer: ByteStreamer = _streamer_by_client[tg_client]

    file_size = file_id.file_size
    range_header = request.headers.get("Range", "")
    start, end = parse_range_header(range_header, file_size)
    req_length = end - start + 1

    # Adaptive chunk size based on this client's recent measured throughput
    chunk_size = get_adaptive_chunk_size(index)
    offset = start - (start % chunk_size)
    first_part_cut = start - offset
    last_part_cut = (end % chunk_size) + 1
    part_count = math.ceil(end / chunk_size) - math.floor(offset / chunk_size)

    from urllib.parse import unquote
    
    stream_id = secrets.token_hex(8)
    
    # Extract original title from the URL path name, fallback to raw name
    decoded_name = unquote(request.path_params.get("name", ""))
    
    # Look up the real title from the database using the Stremio stream_id_hash
    db_title = None
    if stream_id_hash:
        db_title = await db.get_title_by_stream_id(stream_id_hash)
        LOGGER.info(f"Stream lookup for hash '{stream_id_hash}' returned title: {db_title}")
        
    final_title = db_title if db_title else decoded_name
    
    meta = {
        "request_path": str(request.url.path),
        "client_host": request.client.host if request.client else None,
        "title": final_title,
        "user_name": token_data.get("name", "Unknown") if token_data else "Unknown"
    }

    prefetch_count = Telegram.PARALLEL
    parallelism = Telegram.PRE_FETCH

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
        client_pool=client_pool,
    )

    asyncio.create_task(track_usage_from_stats(stream_id, token, token_data))

    file_name = file_id.file_name or f"{secrets.token_hex(4)}.bin"
    mime_type = file_id.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"

    if "." not in file_name and "/" in mime_type:
        file_name = f"{file_name}.{mime_type.split('/')[1]}"

    # HEAD: return headers only (no body), include Content-Length so the
    # client knows the file size without opening a stream.
    # GET: do NOT set Content-Length on the StreamingResponse.
    # If a Telegram chunk fetch times out mid-stream the generator exits early,
    # delivering fewer bytes than the declared length.  h11 enforces
    # Content-Length strictly and raises LocalProtocolError in that case.
    # Without Content-Length, uvicorn uses chunked transfer encoding which
    # handles early termination gracefully.  Stremio / media players
    # are fine with chunked 206 responses.

    # HEAD request support
    from fastapi.responses import Response as PlainResponse

    if request.method == "HEAD":
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(req_length),
            "Content-Disposition": f'inline; filename="{file_name}"',
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        }

        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        return PlainResponse(
            status_code=206 if range_header else 200,
            headers=headers,
        )

    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Content-Length": str(req_length),
        "Cache-Control": "public, max-age=3600",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }

    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        status = 206
    else:
        status = 200

    return StreamingResponse(
        body_gen,
        headers=headers,
        status_code=status,
        media_type=mime_type,
    )


@router.get("/stream/stats")
async def get_stream_stats():
    now = time.time()
    PRUNE_SECONDS = 3

    for sid, info in list(ACTIVE_STREAMS.items()):
        status = info.get("status")
        # Check end_ts first, which is set when a stream organically finishes
        last_ts = info.get("end_ts") or info.get("last_ts") or info.get("start_ts", now)
        if status in ("cancelled", "error", "finished"):
            if now - last_ts > PRUNE_SECONDS:
                try:
                    RECENT_STREAMS.appendleft(ACTIVE_STREAMS.pop(sid))
                except KeyError:
                    pass

    active = []
    for sid, info in ACTIVE_STREAMS.items():
        active.append(
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
        )

    recent = []
    for info in RECENT_STREAMS:
        recent.append(
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
        )

    return JSONResponse(
        {
            "active_streams": active,
            "recent_streams": recent,
            "client_dc_map": client_dc_map,
            "work_loads": work_loads,
        }
    )

@router.get("/stream/stats/{stream_id}")
async def get_stream_detail(stream_id: str):
    info = ACTIVE_STREAMS.get(stream_id)
    if info:
        return JSONResponse(make_json_safe(info))

    for rec in RECENT_STREAMS:
        if rec.get("stream_id") == stream_id:
            return JSONResponse(make_json_safe(rec))

    raise HTTPException(status_code=404, detail="Stream not found")