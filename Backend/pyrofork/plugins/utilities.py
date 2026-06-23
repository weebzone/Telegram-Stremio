"""
Utilities Plugin — stats dashboard.

Commands (owner-only, private chat):
    /stats                  — DB dashboard (counts, sizes, uptime)

Note: the former /search, /dbcheck, /scan, /rescan, /scanstatus and /cancelscan
commands have been migrated to the WebUI "Tools" page (/admin/tools).
"""

import time
from pyrogram import filters, Client, enums
from pyrogram.types import Message

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from Backend import db, StartTime, __version__
from Backend.helper.settings_manager import SettingsManager


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /stats — Quick dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _format_uptime(seconds: int) -> str:
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _format_bytes(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@Client.on_message(filters.command('stats') & filters.private & CustomFilters.owner, group=10)
async def stats_command(client: Client, message: Message):
    """Show a DB and system dashboard."""
    status_msg = await message.reply_text(
        "📊 Gathering stats…", quote=True
    )

    try:
        total_movies = 0
        total_tv = 0
        total_episodes = 0
        total_streams = 0
        total_db_size = 0

        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")
            if storage is None:
                continue

            # Movie counts
            movie_count = await storage["movie"].count_documents({})
            total_movies += movie_count

            # Count movie streams
            async for movie in storage["movie"].find({}, {"telegram": 1}):
                total_streams += len(movie.get("telegram", []))

            # TV counts
            tv_count = await storage["tv"].count_documents({})
            total_tv += tv_count

            # Count episodes and TV streams
            async for show in storage["tv"].find({}, {"seasons": 1}):
                for season in show.get("seasons", []):
                    for episode in season.get("episodes", []):
                        total_episodes += 1
                        total_streams += len(episode.get("telegram", []))

            # DB size
            try:
                db_stats = await storage.command("dbStats")
                total_db_size += db_stats.get("dataSize", 0)
            except Exception:
                pass

        uptime_sec = int(time.time() - StartTime)
        channels_count = len(SettingsManager.current().auth_channels)

        text = (
            f"<blockquote>📊 <b>Telegram-Stremio v{__version__}</b></blockquote>\n\n"
            f"<b>Content</b>\n"
            f"  🎬 Movies: <code>{total_movies}</code>\n"
            f"  📺 TV Shows: <code>{total_tv}</code>\n"
            f"  🎞 Episodes: <code>{total_episodes}</code>\n"
            f"  📁 Total streams: <code>{total_streams}</code>\n\n"
            f"<b>System</b>\n"
            f"  ⏱ Uptime: <code>{_format_uptime(uptime_sec)}</code>\n"
            f"  💾 DB size: <code>{_format_bytes(total_db_size)}</code>\n"
            f"  🗄 Storage DBs: <code>{db.current_db_index}</code>\n"
            f"  📡 AUTH channels: <code>{channels_count}</code>\n"
        )
        await status_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)

    except Exception as e:
        LOGGER.error(f"[Stats] Error: {e}")
        await status_msg.edit_text(f"❌ Error gathering stats: <code>{e}</code>",
                                    parse_mode=enums.ParseMode.HTML)
