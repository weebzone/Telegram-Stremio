"""
tools/ — admin Tools-page backend (scan, dbcheck, duplicates, manual add).

Groups the job managers and helpers driven by /admin/tools:

  - scan_manager       channel scan / rescan
  - dbcheck_manager    verify Telegram messages still exist
  - duplicate_manager  find & purge duplicate streams
  - manual_add helpers resolve posts and stamp captions

Managers are loaded lazily so importing manual_add helpers does not pull
in the full scan stack (avoids circular imports with subtitles).

Example
-------
    from Backend.helper.tools import (
        scan_manager, dbcheck_manager, duplicate_manager
    )
    from Backend.helper.tools.manual_add import resolve_telegram_message
"""

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

_LAZY = {
    "scan_manager": ("Backend.helper.tools.scan", "scan_manager"),
    "dbcheck_manager": ("Backend.helper.tools.dbcheck", "dbcheck_manager"),
    "duplicate_manager": ("Backend.helper.tools.duplicates", "duplicate_manager"),
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib
        mod_name, attr = _LAZY[name]
        mod = importlib.import_module(mod_name)
        value = getattr(mod, attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
