"""
Active and recent stream registry with automatic stale cleanup.

Maintains the global ACTIVE_STREAMS and RECENT_STREAMS collections used by
ByteStreamer, analytics, dashboard templates and routes. A background task
periodically moves idle or finished streams out of the active set so load
counters stay accurate.
"""

import asyncio
import time
from collections import deque
from typing import Dict

from Backend.pyrofork.bot import work_loads

ACTIVE_STREAMS: Dict[str, Dict] = {}
RECENT_STREAMS = deque(maxlen=20)
STALE_STREAM_IDLE = 180
_STALE_CLEANER_STARTED = False


async def _cleanup_stale_streams():
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            stale = []
            for sid, entry in list(ACTIVE_STREAMS.items()):
                last = entry.get("last_ts") or entry.get("start_ts") or 0
                status = entry.get("status") or "active"
                total = entry.get("total_bytes") or 0
                idle = now - last
                if status != "active" or idle > STALE_STREAM_IDLE or (total == 0 and idle > 60):
                    stale.append(sid)
            for sid in stale:
                try:
                    entry = ACTIVE_STREAMS.pop(sid, None)
                    if entry:
                        entry["status"] = "stale"
                        entry["end_ts"] = now
                        RECENT_STREAMS.appendleft(entry)
                        idx = entry.get("client_index")
                        if idx is not None and idx in work_loads:
                            work_loads[idx] = max(0, work_loads[idx] - 1)
                except Exception:
                    pass
        except Exception:
            pass


def _ensure_stale_cleaner():
    global _STALE_CLEANER_STARTED
    if not _STALE_CLEANER_STARTED:
        _STALE_CLEANER_STARTED = True
        asyncio.create_task(_cleanup_stale_streams())
