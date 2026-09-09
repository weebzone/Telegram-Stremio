"""
Streaming package for Telegram-Stremio.

Provides byte-range streaming over Telegram media (single files, multi-part
virtual concatenations, and zip entries) via ByteStreamer and related helpers.

Modules:
  - custom_dl      : ByteStreamer core engine
  - registry       : ACTIVE_STREAMS / RECENT_STREAMS + stale cleanup
  - speed_test     : client throughput measurement helpers
  - virtual_dl     : multi-part (split) stream stitching
  - zip_stream     : on-the-fly ZIP entry extraction

Public surface:
  - ACTIVE_STREAMS, RECENT_STREAMS
  - ByteStreamer
  - resolve_virtual_parts, virtual_stream_generator
  - resolve_zip_entry
  - _speed_test_single_client, run_speed_test
"""

from Backend.helper.streaming.custom_dl import ByteStreamer
from Backend.helper.streaming.registry import ACTIVE_STREAMS, RECENT_STREAMS
from Backend.helper.streaming.speed_test import _speed_test_single_client, run_speed_test
from Backend.helper.streaming.virtual_dl import (
    resolve_virtual_parts,
    virtual_stream_generator,
)
from Backend.helper.streaming.zip_stream import resolve_zip_entry

__all__ = [
    "ACTIVE_STREAMS",
    "RECENT_STREAMS",
    "ByteStreamer",
    "_speed_test_single_client",
    "run_speed_test",
    "resolve_virtual_parts",
    "virtual_stream_generator",
    "resolve_zip_entry",
]
