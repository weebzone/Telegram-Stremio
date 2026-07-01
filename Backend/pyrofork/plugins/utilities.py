import time

from pyrogram import Client, enums, filters
from pyrogram.types import Message

from Backend import StartTime, __version__, db
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.pyro import get_readable_file_size, get_readable_time
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Owner-only /stats: aggregate content and system metrics across DBs
@Client.on_message(filters.command('stats') & filters.private & CustomFilters.owner, group=10)
async def stats_command(client: Client, message: Message):
    status_msg = await message.reply_text("📊 Gathering stats…", quote=True)

    try:
        total_movies = total_tv = total_episodes = total_streams = total_db_size = 0

        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")
            if storage is None:
                continue

            total_movies += await storage["movie"].count_documents({})
            async for movie in storage["movie"].find({}, {"telegram": 1}):
                total_streams += len(movie.get("telegram", []))

            total_tv += await storage["tv"].count_documents({})
            async for show in storage["tv"].find({}, {"seasons": 1}):
                for season in show.get("seasons", []):
                    for episode in season.get("episodes", []):
                        total_episodes += 1
                        total_streams += len(episode.get("telegram", []))

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
            f"  ⏱ Uptime: <code>{get_readable_time(uptime_sec)}</code>\n"
            f"  💾 DB size: <code>{get_readable_file_size(total_db_size)}</code>\n"
            f"  🗄 Storage DBs: <code>{db.current_db_index}</code>\n"
            f"  📡 AUTH channels: <code>{channels_count}</code>\n"
        )
        await status_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)

    except Exception as e:
        LOGGER.error(f"[Stats] Error: {e}")
        await status_msg.edit_text(f"❌ Error gathering stats: <code>{e}</code>", parse_mode=enums.ParseMode.HTML)
