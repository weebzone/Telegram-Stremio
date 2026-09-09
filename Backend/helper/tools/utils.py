"""
tools/utils.py — shared helpers for Tools (scan, dbcheck, duplicates).

Small utilities used by every Tools manager: timestamps, elapsed-time
formatting, and the Mongo collection name used to persist job state.

Example
-------
    from Backend.helper.tools.utils import now, fmt_elapsed, STATE_COLLECTION

    started = now()
    ...
    print(fmt_elapsed(now() - started))  # "2m 15s"
"""

from __future__ import annotations

import time

STATE_COLLECTION = "scan_state"
SCAN_DOC_ID = "scan"


def now() -> float:
    return time.time()


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"
