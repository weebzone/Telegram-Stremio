import asyncio

from Backend import db
from Backend.helper.custom_dl import ACTIVE_STREAMS, RECENT_STREAMS
from Backend.logger import LOGGER


#----- Fold the peak byte count across a stream and its split parts; report if still active
def _collect_stream_bytes(stream_id: str, seen: dict) -> bool:
    prefix = f"{stream_id}-p"

    def matches(sid):
        return sid == stream_id or (sid and sid.startswith(prefix))

    active = False
    for sid, info in ACTIVE_STREAMS.items():
        if matches(sid):
            seen[sid] = max(seen.get(sid, 0), info.get("total_bytes", 0))
            active = True
    for rec in RECENT_STREAMS:
        sid = rec.get("stream_id")
        if matches(sid):
            seen[sid] = max(seen.get(sid, 0), rec.get("total_bytes", 0))
    return active


#----- Periodically accrue a token's bandwidth usage until the stream goes quiet
async def track_usage(stream_id: str, token: str, token_data: dict):
    await asyncio.sleep(2)
    limits = token_data.get("limits", {}) if token_data else {}
    usage = token_data.get("usage", {}) if token_data else {}
    daily_limit_gb = limits.get("daily_limit_gb")
    monthly_limit_gb = limits.get("monthly_limit_gb")
    initial_daily_bytes = usage.get("daily", {}).get("bytes", 0)
    initial_monthly_bytes = usage.get("monthly", {}).get("bytes", 0)

    seen: dict = {}
    last_tracked_bytes = 0
    started = False
    idle_polls = 0
    update_interval = 10

    async def flush(current: int):
        nonlocal last_tracked_bytes
        delta = current - last_tracked_bytes
        if delta > 0:
            try:
                await db.update_token_usage(token, delta)
                last_tracked_bytes = current
            except Exception as e:
                LOGGER.error(f"Usage update failed: {e}")

    try:
        while True:
            await asyncio.sleep(update_interval)
            active = _collect_stream_bytes(stream_id, seen)
            current_bytes = sum(seen.values())
            await flush(current_bytes)

            if daily_limit_gb and daily_limit_gb > 0:
                if (initial_daily_bytes + current_bytes) / (1024 ** 3) >= daily_limit_gb:
                    LOGGER.debug(f"Daily limit reached for stream {stream_id}")
            if monthly_limit_gb and monthly_limit_gb > 0:
                if (initial_monthly_bytes + current_bytes) / (1024 ** 3) >= monthly_limit_gb:
                    LOGGER.debug(f"Monthly limit reached for stream {stream_id}")

            if active:
                started = True
                idle_polls = 0
            else:
                #----- Exit once a started stream goes quiet, or never appears in the grace window
                idle_polls += 1
                if (started and idle_polls >= 2) or (not started and idle_polls >= 6):
                    return
    except asyncio.CancelledError:
        _collect_stream_bytes(stream_id, seen)
        await flush(sum(seen.values()))
