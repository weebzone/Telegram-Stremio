"""
tools/ — admin Tools-page backend (scan, dbcheck, duplicates, manual add).

Groups the job managers and helpers driven by /admin/tools:

  - scan_manager       channel scan / rescan
  - dbcheck_manager    verify Telegram messages still exist
  - duplicate_manager  find & purge duplicate streams
  - manual_add helpers resolve posts and stamp captions

Example
-------
    from Backend.helper.tools import (
        scan_manager, dbcheck_manager, duplicate_manager
    )
    from Backend.helper.tools.manual_add import resolve_telegram_message
"""

from Backend.helper.tools.scan import scan_manager
from Backend.helper.tools.dbcheck import dbcheck_manager
from Backend.helper.tools.duplicates import duplicate_manager
from Backend.helper.tools.manual_add import (
    parse_telegram_link,
    quality_from_height,
    resolve_telegram_message,
    stamp_caption_with_id,
    stamp_caption_by_ref,
)

__all__ = [
    "scan_manager",
    "dbcheck_manager",
    "duplicate_manager",
    "parse_telegram_link",
    "quality_from_height",
    "resolve_telegram_message",
    "stamp_caption_with_id",
    "stamp_caption_by_ref",
]
