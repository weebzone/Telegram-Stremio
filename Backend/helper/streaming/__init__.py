"""
streaming/ — Telegram media streaming engine and helpers.
"""

from Backend.helper.streaming.byte_streamer import ByteStreamer
from Backend.helper.streaming.registry import ACTIVE_STREAMS, RECENT_STREAMS
from Backend.helper.streaming.speed_test import _speed_test_single_client, run_speed_test
from Backend.helper.streaming.virtual_dl import resolve_virtual_parts, virtual_stream_generator
from Backend.helper.streaming.zip_stream import resolve_zip_entry

__all__ = [
    "ByteStreamer",
    "ACTIVE_STREAMS",
    "RECENT_STREAMS",
    "_speed_test_single_client",
    "run_speed_test",
    "resolve_virtual_parts",
    "virtual_stream_generator",
    "resolve_zip_entry",
]
