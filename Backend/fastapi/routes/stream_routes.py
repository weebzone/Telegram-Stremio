import math
import secrets
import mimetypes
import time
from Backend import db
from typing import Tuple
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse

from Backend.helper.encrypt import decode_string
from Backend.helper.exceptions import InvalidHash
from Backend.helper.custom_dl import ByteStreamer
from Backend.pyrofork.bot import StreamBot, work_loads, multi_clients
from Backend.fastapi.security.tokens import verify_token

router = APIRouter(tags=["Streaming"])
class_cache = {}


def parse_range_header(range_header: str, file_size: int) -> Tuple[int, int]:
    if not range_header:
        return 0, file_size - 1
    try:
        range_value = range_header.replace("bytes=", "")
        from_str, until_str = range_value.split("-")
        from_bytes = int(from_str)
        until_bytes = int(until_str) if until_str else file_size - 1
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Range header: {e}")

    if (until_bytes > file_size - 1) or (from_bytes < 0) or (until_bytes < from_bytes):
        raise HTTPException(
            status_code=416,
            detail="Requested Range Not Satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    return from_bytes, until_bytes


@router.get("/dl/{token}/{id}/{name}")
@router.head("/dl/{token}/{id}/{name}")
async def stream_handler(request: Request, token: str, id: str, name: str, token_data: dict = Depends(verify_token)):
    decoded_data = await decode_string(id)
    if not decoded_data.get("msg_id"):
        raise HTTPException(status_code=400, detail="Missing id")

    chat_id = f"-100{decoded_data['chat_id']}"
    message = await StreamBot.get_messages(int(chat_id), int(decoded_data["msg_id"]))
    file = message.video or message.document
    file_hash = file.file_unique_id[:6]

    return await media_streamer(
        request,
        chat_id=int(chat_id),
        id=int(decoded_data["msg_id"]),
        secure_hash=file_hash,
        token_data=token_data
    )


async def media_streamer(
    request: Request,
    chat_id: int,
    id: int,
    secure_hash: str,
    token_data: dict = None
) -> StreamingResponse:
    range_header = request.headers.get("Range", "")
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    tg_connect = class_cache.get(faster_client)
    if not tg_connect:
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect

    file_id = await tg_connect.get_file_properties(chat_id=chat_id, message_id=id)
    if file_id.unique_id[:6] != secure_hash:
        raise InvalidHash

    file_size = file_id.file_size
    from_bytes, until_bytes = parse_range_header(range_header, file_size)

    chunk_size = 1024 * 1024
    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = (until_bytes % chunk_size) + 1
    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)

    
    # Wrapper to count bytes and limit usage
    async def stream_generator(file_stream, token):
        start_time = time.time()
        byte_count = 0
        update_interval = 60  # 1 minute

        # Extract limits
        limits = token_data.get("limits", {}) if token_data else {}
        usage = token_data.get("usage", {}) if token_data else {}
        
        daily_limit_gb = limits.get("daily_limit_gb")
        monthly_limit_gb = limits.get("monthly_limit_gb")
        
        # Initial usage (Bytes)
        initial_daily_bytes = usage.get("daily", {}).get("bytes", 0)
        initial_monthly_bytes = usage.get("monthly", {}).get("bytes", 0)
        
        # Session accumulator for limit checking
        session_total = 0

        async for chunk in file_stream:
            chunk_len = len(chunk)
            byte_count += chunk_len
            session_total += chunk_len
            
            # Check Limits (Enforce loop)
            if daily_limit_gb and daily_limit_gb > 0:
                current_daily_gb = (initial_daily_bytes + session_total) / (1024**3)
                if current_daily_gb >= daily_limit_gb:
                    # Update DB before killing
                    if byte_count > 0:
                        await db.update_token_usage(token, byte_count)
                    # Stop stream
                    return

            if monthly_limit_gb and monthly_limit_gb > 0:
                current_monthly_gb = (initial_monthly_bytes + session_total) / (1024**3)
                if current_monthly_gb >= monthly_limit_gb:
                    if byte_count > 0:
                        await db.update_token_usage(token, byte_count)
                    return

            yield chunk
            
            # Check interval
            if (time.time() - start_time) > update_interval:
                if byte_count > 0:
                     await db.update_token_usage(token, byte_count)
                     byte_count = 0
                start_time = time.time() # Reset timer
        
        # Final update
        if byte_count > 0:
            await db.update_token_usage(token, byte_count)

    body = stream_generator(
        tg_connect.yield_file(
            file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
        ),
        request.path_params.get("token") # Get token from path
    )

    file_name = file_id.file_name or f"{secrets.token_hex(2)}.unknown"
    mime_type = file_id.mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    if not file_id.file_name and "/" in mime_type:
        file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"

    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(req_length),
        "Content-Disposition": f'inline; filename="{file_name}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600, immutable",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    
    if range_header:
        headers["Content-Range"] = f"bytes {from_bytes}-{until_bytes}/{file_size}"
        status_code = 206
    else:
        status_code = 200
    
    return StreamingResponse(
        status_code=status_code,
        content=body,
        headers=headers,
        media_type=mime_type,
    )