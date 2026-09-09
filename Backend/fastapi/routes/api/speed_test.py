"""
api/speed_test.py — client throughput speed-test endpoints.
"""

import asyncio
import json
import os
import random
import secrets
import shutil
from datetime import datetime
from time import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pyrogram.enums import ChatMemberStatus, ChatMembersFilter
from pyrogram.errors import FloodWait
from pyrogram.types import ChatPrivileges

import Backend
from Backend import StartTime, __version__, db
from Backend.fastapi.routes.stream_routes import _streamer_by_client
from Backend.fastapi.routes.stremio_routes import invalidate_membership_cache
from Backend.helper.analytics import get_activity_overview
from Backend.helper.auto_catalog import (
    get_auto_catalog_settings,
    get_auto_catalog_sync_status,
    start_auto_catalog_sync_background,
    start_single_media_catalog_sync,
    update_auto_catalog_settings,
)
from Backend.helper.tasks.backup import export_config, import_config
from Backend.helper.streaming.byte_streamer import ByteStreamer
from Backend.helper.streaming.speed_test import _speed_test_single_client, run_speed_test
from Backend.helper.encrypt import decode_string, encode_string
from Backend.helper.health import run_health_checks
from Backend.helper.tools.manual_add import resolve_telegram_message, stamp_caption_by_ref
from Backend.helper.requests_manager import (
    delete_request,
    list_requests,
    popular_pending,
    search_titles,
    set_status,
    submit_request,
)
from Backend.helper.metadata import (
    extract_default_id,
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    gradient_cover_path,
    resolve_cover_url,
    search_any_candidates,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.passwords import hash_password, verify_password
from Backend.helper.pyro import get_readable_file_size, get_readable_time
from Backend.helper.tools import dbcheck_manager, duplicate_manager, scan_manager
from Backend.helper.session_auth import (
    disconnect_session,
    get_session_status,
    reconnect_session,
    remove_session,
    start_login,
    submit_code,
    submit_password,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.split_files import strip_part_suffix
from Backend.helper.subtitles import (
    list_languages,
    list_title_subtitles,
    manual_ingest_subtitle,
    remove_subtitle,
    resolve_subtitle_message,
)
from Backend.logger import LOGGER
import Backend.pyrofork.bot as botmod
from Backend.helper.announcer import delete_announcement_async
from Backend.pyrofork.bot import (
    StreamBot,
    client_avg_mbps,
    client_dc_map,
    client_failures,
    multi_clients,
    work_loads,
)

from Backend.fastapi.routes.api._helpers import (
    _require_tmdb_id,
    _resolve_speed_test_target,
)

async def speed_test_api(
    quality_id: str = Query(..., description="Encoded quality ID from DB"),
    tmdb_id: str | int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(..., regex="^(movie|tv)$"),
):
    tmdb_id = _require_tmdb_id(tmdb_id)
    try:
        chat_id, msg_id, decoded = await _resolve_speed_test_target(quality_id)
        if not chat_id or not msg_id:
            raise HTTPException(
                status_code=422,
                detail=f"Decoded quality data is missing msg_id or chat_id. Decoded: {decoded}"
            )

        results = await run_speed_test(chat_id, msg_id)
        return {"results": results, "total_clients_tested": len(results)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def speed_test_stream_api(
    quality_id: str,
    tmdb_id: str | int,
    db_index: int,
    media_type: str,
):

    tmdb_id = _require_tmdb_id(tmdb_id)
    async def event_generator():
        try:
            chat_id, msg_id, decoded = await _resolve_speed_test_target(quality_id)
            if not chat_id or not msg_id:
                payload = json.dumps({"type": "error", "message": f"Cannot decode quality_id. Got: {decoded}"})
                yield f"data: {payload}\n\n"
                return
        except Exception as exc:
            payload = json.dumps({"type": "error", "message": str(exc)})
            yield f"data: {payload}\n\n"
            return

        total = len(multi_clients)
        if total == 0:
            payload = json.dumps({"type": "error", "message": "No bot clients connected"})
            yield f"data: {payload}\n\n"
            return

        target_dc = "?"
        try:
            primary_client = multi_clients.get(0) or next(iter(multi_clients.values()))
            streamer = ByteStreamer(primary_client)
            file_id = await streamer.get_file_properties(chat_id, int(msg_id))
            target_dc = file_id.dc_id
        except Exception:
            pass

        yield f"data: {json.dumps({'type': 'start', 'total': total, 'target_dc': target_dc})}\n\n"

        queue: asyncio.Queue = asyncio.Queue()

        async def run_one(client, idx):
            async def on_progress(prog_data):
                await queue.put({"type": "progress", "data": prog_data})

            result = await _speed_test_single_client(
                client, idx, chat_id, int(msg_id), progress_callback=on_progress
            )
            await queue.put({"type": "result", "data": result})

        tasks = [
            asyncio.create_task(run_one(client, idx))
            for idx, client in multi_clients.items()
        ]

        completed = 0
        while completed < total:
            msg = await queue.get()

            if msg["type"] == "progress":
                payload = json.dumps(msg)
                yield f"data: {payload}\n\n"

            elif msg["type"] == "result":
                completed += 1
                payload = json.dumps({
                    "type": "result",
                    "data": msg["data"],
                    "completed": completed,
                    "total": total,
                })
                yield f"data: {payload}\n\n"

        await asyncio.gather(*tasks, return_exceptions=True)
        yield f"data: {json.dumps({'type': 'done', 'total': total})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
