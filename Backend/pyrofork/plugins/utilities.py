"""
Utilities Plugin — stats, search, DB integrity check, channel backup/restore.

Commands (owner-only, private chat):
    /stats                  — DB dashboard (counts, sizes, uptime)
    /search <title>         — Search your DB for a movie or show
    /dbcheck                — Find orphaned stream entries (dead Telegram messages)
    /dbcheck purge          — Find and delete orphaned entries
    /exportchannels         — Export AUTH_CHANNEL list as JSON
    /importchannels <json>  — Import channels from JSON
"""

import asyncio
import json
import time
from pyrogram import filters, Client, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from pymongo.errors import CursorNotFound

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from Backend import db, StartTime, __version__
from Backend.config import Telegram
from Backend.helper.encrypt import decode_string


CONCURRENT_TASKS = 10
BATCH_SIZE = 100
PROGRESS_UPDATE_EVERY = 50

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
        channels_count = len(Telegram.AUTH_CHANNEL)

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /search <title> — Search own DB
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@Client.on_message(filters.command('search') & filters.private & CustomFilters.owner, group=10)
async def search_command(client: Client, message: Message):
    """Search the DB for movies or TV shows by title."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage: /search <code>&lt;title&gt;</code>\n\n"
            "Example: <code>/search Breaking Bad</code>",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    query = args[1].strip()
    import re
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    regex_filter = {"title": {"$regex": pattern.pattern, "$options": "i"}}
    results = []

    for i in range(1, db.current_db_index + 1):
        storage = db.dbs.get(f"storage_{i}")
        if storage is None:
            continue

        # Search movies
        try:
            movies = await storage["movie"].find(regex_filter).to_list(length=10)
            for movie in movies:
                qualities = movie.get("telegram", [])
                q_list = ", ".join(q.get("quality", "?") for q in qualities) or "none"
                results.append(
                    f"🎬 <b>{movie.get('title', '?')}</b> ({movie.get('release_year', '?')})\n"
                    f"   TMDB: <code>{movie.get('tmdb_id', '?')}</code> | "
                    f"IMDb: <code>{movie.get('imdb_id', '?')}</code>\n"
                    f"   Qualities: {q_list} | DB: storage_{i}"
                )
        except Exception as e:
            LOGGER.error(f"[Search] Movie search error in storage_{i}: {e}")

        # Search TV shows
        try:
            shows = await storage["tv"].find(regex_filter).to_list(length=10)
            for show in shows:
                seasons = show.get("seasons", [])
                ep_count = sum(len(s.get("episodes", [])) for s in seasons)
                stream_count = sum(
                    len(ep.get("telegram", []))
                    for s in seasons for ep in s.get("episodes", [])
                )
                season_nums = sorted(s.get("season_number", 0) for s in seasons)
                s_range = f"S{season_nums[0]}–S{season_nums[-1]}" if season_nums else "?"
                results.append(
                    f"📺 <b>{show.get('title', '?')}</b> ({show.get('release_year', '?')})\n"
                    f"   TMDB: <code>{show.get('tmdb_id', '?')}</code> | "
                    f"IMDb: <code>{show.get('imdb_id', '?')}</code>\n"
                    f"   {s_range} | {ep_count} episodes | {stream_count} streams | DB: storage_{i}"
                )
        except Exception as e:
            LOGGER.error(f"[Search] TV search error in storage_{i}: {e}")

    if not results:
        # Debug: check if DB has any data at all
        debug = ""
        try:
            for i in range(1, db.current_db_index + 1):
                storage = db.dbs.get(f"storage_{i}")
                if storage is None:
                    continue
                mc = await storage["movie"].count_documents({})
                tc = await storage["tv"].count_documents({})
                # Grab a sample title to confirm data exists
                sample = await storage["movie"].find_one({}, {"title": 1})
                sample_title = sample.get("title", "?") if sample else "empty"
                debug = f"\n\n<i>Debug: storage_{i} has {mc} movies, {tc} tv. Sample: {sample_title}</i>"
        except Exception:
            pass

        await message.reply_text(
            f"🔍 No results for <b>{query}</b>{debug}",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    header = f"<blockquote>🔍 <b>Search results for:</b> {query}</blockquote>\n\n"
    body = "\n\n".join(results[:15])  # cap at 15 results
    footer = ""
    if len(results) > 15:
        footer = f"\n\n<i>…and {len(results) - 15} more. Try a more specific query.</i>"

    await message.reply_text(
        header + body + footer,
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Check single Telegram message
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def check_message(client, stream_hash):
    try:
        decoded = await decode_string(stream_hash)
        chat_id = int(f"-100{decoded['chat_id']}")
        msg_id = int(decoded['msg_id'])

        msg = await client.get_messages(chat_id, msg_id)

        if msg.empty:
            return False
        return True

    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await check_message(client, stream_hash)

    except Exception:
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Batch processor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def process_batch(client, batch):
    tasks = [check_message(client, s) for s in batch]
    return await asyncio.gather(*tasks, return_exceptions=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# /dbcheck — Integrity checker (find orphaned streams)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@Client.on_message(filters.command('dbcheck') & filters.private & CustomFilters.owner, group=10)
async def dbcheck_command(client: Client, message: Message):

    args = message.text.split()
    purge_mode = len(args) > 1 and args[1].lower() == "purge"

    status_msg = await message.reply_text(
        "🚀 DB Check started (Atlas-safe mode)...",
        quote=True
    )

    checked = 0
    dead = 0
    alive = 0
    errors = 0
    purged = 0

    dead_entries = []

    start_time = time.time()

    try:
        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")

            if storage is None:
                continue

            # ───────── MOVIES (PAGINATION) ─────────
            last_id = None

            while True:
                query = {"_id": {"$gt": last_id}} if last_id else {}

                docs = await storage["movie"] \
                    .find(query) \
                    .sort("_id", 1) \
                    .limit(BATCH_SIZE) \
                    .to_list(length=BATCH_SIZE)

                if not docs:
                    break

                for movie in docs:
                    last_id = movie["_id"]
                    title = movie.get("title", "Unknown")

                    stream_ids = [
                        q.get("id") for q in movie.get("telegram", [])
                        if q.get("id")
                    ]

                    for x in range(0, len(stream_ids), CONCURRENT_TASKS):
                        batch = stream_ids[x:x + CONCURRENT_TASKS]
                        results = await process_batch(client, batch)

                        for stream_hash, result in zip(batch, results):
                            checked += 1

                            if result is True:
                                alive += 1
                            elif result is False:
                                dead += 1
                                dead_entries.append(stream_hash)
                            else:
                                errors += 1

                        if checked % PROGRESS_UPDATE_EVERY == 0:
                            elapsed = int(time.time() - start_time)
                            speed = checked // max(1, elapsed)

                            await status_msg.edit_text(
                                f"🚀 DB Check Running...\n\n"
                                f"Checked: {checked}\n"
                                f"Alive: {alive}\n"
                                f"Dead: {dead}\n"
                                f"Errors: {errors}\n\n"
                                f"⚡ Speed: {speed}/sec"
                            )

            # ───────── TV (PAGINATION) ─────────
            last_id = None

            while True:
                query = {"_id": {"$gt": last_id}} if last_id else {}

                docs = await storage["tv"] \
                    .find(query) \
                    .sort("_id", 1) \
                    .limit(BATCH_SIZE) \
                    .to_list(length=BATCH_SIZE)

                if not docs:
                    break

                for show in docs:
                    last_id = show["_id"]

                    stream_ids = []

                    for season in show.get("seasons", []):
                        for episode in season.get("episodes", []):
                            for q in episode.get("telegram", []):
                                if q.get("id"):
                                    stream_ids.append(q["id"])

                    for x in range(0, len(stream_ids), CONCURRENT_TASKS):
                        batch = stream_ids[x:x + CONCURRENT_TASKS]
                        results = await process_batch(client, batch)

                        for stream_hash, result in zip(batch, results):
                            checked += 1

                            if result is True:
                                alive += 1
                            elif result is False:
                                dead += 1
                                dead_entries.append(stream_hash)
                            else:
                                errors += 1

                        if checked % PROGRESS_UPDATE_EVERY == 0:
                            elapsed = int(time.time() - start_time)
                            speed = checked // max(1, elapsed)

                            await status_msg.edit_text(
                                f"🚀 DB Check Running...\n\n"
                                f"Checked: {checked}\n"
                                f"Alive: {alive}\n"
                                f"Dead: {dead}\n"
                                f"Errors: {errors}\n\n"
                                f"⚡ Speed: {speed}/sec"
                            )

        # ───────── PURGE ─────────
        if purge_mode and dead_entries:
            purge_tasks = []

            for stream_hash in dead_entries:
                purge_tasks.append(db.delete_media_by_stream_id(stream_hash))

                if len(purge_tasks) >= CONCURRENT_TASKS:
                    results = await asyncio.gather(*purge_tasks, return_exceptions=True)
                    purged += sum(1 for r in results if r)
                    purge_tasks = []

            if purge_tasks:
                results = await asyncio.gather(*purge_tasks, return_exceptions=True)
                purged += sum(1 for r in results if r)

        # ───────── FINAL RESULT ─────────
        elapsed = int(time.time() - start_time)

        await status_msg.edit_text(
            f"✅ DB CHECK COMPLETE\n\n"
            f"Checked: {checked}\n"
            f"Alive: {alive}\n"
            f"Dead: {dead}\n"
            f"Errors: {errors}\n\n"
            f"🗑 Purged: {purged}\n"
            f"⏱ Time: {elapsed}s\n"
            f"⚡ Avg Speed: {checked // max(1, elapsed)}/sec"
        )

    except Exception as e:
        LOGGER.error(f"[DBCheck] Error: {e}")
        await status_msg.edit_text(f"❌ DB check failed: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /exportchannels & /importchannels — Backup/restore channel config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@Client.on_message(filters.command('exportchannels') & filters.private & CustomFilters.owner, group=10)
async def export_channels(client: Client, message: Message):
    """Export current AUTH_CHANNEL list as a copyable JSON block."""
    channels_data = []
    for ch_id in Telegram.AUTH_CHANNEL:
        entry = {"id": ch_id}
        try:
            chat = await client.get_chat(int(ch_id))
            entry["name"] = getattr(chat, "title", "")
            entry["members"] = getattr(chat, "members_count", 0)
        except Exception:
            entry["name"] = "[inaccessible]"
        channels_data.append(entry)

    export = {
        "version": __version__,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "channels": channels_data,
    }

    json_str = json.dumps(export, indent=2, ensure_ascii=False)

    await message.reply_text(
        f"<blockquote>📤 <b>Channel Export</b></blockquote>\n\n"
        f"<pre>{json_str}</pre>\n\n"
        f"<i>Use /importchannels to restore this on another instance.</i>",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command('importchannels') & filters.private & CustomFilters.owner, group=10)
async def import_channels(client: Client, message: Message):
    """Import channels from a JSON export block."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage: /importchannels <code>&lt;json&gt;</code>\n\n"
            "Paste the JSON output from /exportchannels after the command.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    raw = args[1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        await message.reply_text(
            f"❌ Invalid JSON: <code>{e}</code>",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    channels = data.get("channels", [])
    if not channels:
        await message.reply_text("❌ No channels found in the JSON.", quote=True)
        return

    added = 0
    skipped = 0
    failed = 0

    for entry in channels:
        ch_id = str(entry.get("id", "")).strip()
        if not ch_id:
            continue

        if ch_id in Telegram.AUTH_CHANNEL:
            skipped += 1
            continue

        # Validate access
        try:
            await client.get_chat(int(ch_id))
            Telegram.AUTH_CHANNEL.append(ch_id)
            added += 1
        except Exception:
            failed += 1

    # Persist
    if added > 0:
        from Backend.pyrofork.plugins.channels import _save_channels_to_db
        await _save_channels_to_db()

    await message.reply_text(
        f"<blockquote>📥 <b>Channel Import Complete</b></blockquote>\n\n"
        f"✅ Added: <code>{added}</code>\n"
        f"⏭ Skipped (already exists): <code>{skipped}</code>\n"
        f"❌ Failed (no access): <code>{failed}</code>\n\n"
        f"Total AUTH_CHANNELs: <code>{len(Telegram.AUTH_CHANNEL)}</code>",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )
