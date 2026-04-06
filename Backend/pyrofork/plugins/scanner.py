"""
Channel Scanner Plugin — indexes existing channel content into the database.

Commands (owner-only, private chat):
    /scan          — Scan all AUTH_CHANNELs, skip already-indexed messages
    /scan <id>     — Scan a specific channel by numeric ID
    /rescan        — Wipe DB entries for AUTH_CHANNELs, then full re-index
    /scanstatus    — Show progress of any running scan
    /cancelscan    — Abort a running scan gracefully
"""

import asyncio
import time
from pyrogram import filters, Client, enums
from pyrogram.types import Message
from pyrogram.errors import FloodWait, ChannelPrivate, ChatAdminRequired

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from Backend import db
from Backend.config import Telegram
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.metadata import metadata
from Backend.helper.encrypt import encode_string


# ── Scan state (singleton — only one scan at a time) ────────────────────
class _ScanState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.cancelled = False
        self.channel_id = None
        self.channel_name = ""
        self.total_found = 0
        self.processed = 0
        self.indexed = 0
        self.skipped_dup = 0
        self.skipped_meta = 0
        self.skipped_nonvid = 0
        self.errors = 0
        self.started_at = 0.0
        self.status_msg: Message | None = None

    @property
    def elapsed(self) -> str:
        s = int(time.time() - self.started_at) if self.started_at else 0
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


scan_state = _ScanState()

PROGRESS_EVERY = 15  
RATE_LIMIT_DELAY = 0.3 


# ── Helpers ──────────────────────────────────────────────────────────────

async def _stream_id_exists(channel: int, msg_id: int) -> bool:
    """Check if this (channel, msg_id) combo is already in the DB."""
    try:
        stream_hash = await encode_string({"chat_id": channel, "msg_id": msg_id})
    except Exception:
        return False

    # Check across all storage DBs
    for i in range(1, db.current_db_index + 1):
        storage = db.dbs.get(f"storage_{i}")
        if storage is None:
            continue
        if await storage["movie"].find_one({"telegram.id": stream_hash}):
            return True
        if await storage["tv"].find_one({"seasons.episodes.telegram.id": stream_hash}):
            return True
    return False


async def _update_progress(force: bool = False):
    """Edit the status message with current scan progress."""
    s = scan_state
    if not s.status_msg:
        return
    if not force and s.processed % PROGRESS_EVERY != 0:
        return
    try:
        text = (
            f"<blockquote>📡 <b>Scanning:</b> {s.channel_name}</blockquote>\n\n"
            f"⏱ Elapsed: <code>{s.elapsed}</code>\n"
            f"📨 Processed: <code>{s.processed}</code>\n"
            f"✅ Indexed: <code>{s.indexed}</code>\n"
            f"⏭ Skipped (duplicate): <code>{s.skipped_dup}</code>\n"
            f"⚠️ Skipped (metadata fail): <code>{s.skipped_meta}</code>\n"
            f"📎 Skipped (non-video): <code>{s.skipped_nonvid}</code>\n"
            f"❌ Errors: <code>{s.errors}</code>"
        )
        await s.status_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass  


async def _scan_channel(client: Client, chat_id: int):
    """Iterate all video messages in a channel and index them.
    
    Bots cannot use get_chat_history or search_messages (both user-only).
    The only reliable bot method is get_messages with explicit IDs.
    
    Strategy: walk forward from ID 1 in batches of 200. Keep going even
    through empty batches (deleted messages / gaps). Stop after 5
    consecutive all-empty batches, which tolerates gaps of up to ~1000 IDs.
    Hard cap at 500,000 to prevent infinite loops.
    """
    s = scan_state

    try:
        chat = await client.get_chat(chat_id)
        s.channel_name = getattr(chat, "title", str(chat_id))
    except (ChannelPrivate, ChatAdminRequired) as e:
        LOGGER.error(f"Cannot access channel {chat_id}: {e}")
        raise
    except Exception as e:
        s.channel_name = str(chat_id)
        LOGGER.warning(f"Could not resolve channel name for {chat_id}: {e}")

    s.channel_id = chat_id
    LOGGER.info(f"[Scanner] Starting scan of {s.channel_name} ({chat_id})")

    BATCH_SIZE = 200
    MAX_EMPTY_BATCHES = 5      
    MAX_ID_CAP = 500_000      
    empty_streak = 0
    current = 1

    while empty_streak < MAX_EMPTY_BATCHES and current < MAX_ID_CAP:
        if s.cancelled:
            LOGGER.info("[Scanner] Scan cancelled by user.")
            break

        batch_ids = list(range(current, min(current + BATCH_SIZE, MAX_ID_CAP)))

        try:
            messages = await client.get_messages(chat_id, batch_ids)
        except FloodWait as e:
            LOGGER.info(f"[Scanner] FloodWait {e.value}s, sleeping…")
            await asyncio.sleep(e.value)
            try:
                messages = await client.get_messages(chat_id, batch_ids)
            except Exception as ex:
                LOGGER.error(f"[Scanner] Retry failed at {current}: {ex}")
                s.errors += 1
                current += BATCH_SIZE
                empty_streak += 1
                continue
        except Exception as e:
            LOGGER.error(f"[Scanner] Batch fetch error at {current}: {e}")
            s.errors += 1
            current += BATCH_SIZE
            empty_streak += 1
            continue

        if not isinstance(messages, list):
            messages = [messages]

        batch_had_content = False

        for message in messages:
            if s.cancelled:
                break

            if message.empty:
                continue

            batch_had_content = True
            s.total_found += 1

            # Only process videos / video documents
            is_video = bool(message.video)
            is_video_doc = False
            if message.document and not is_video:
                mime = getattr(message.document, "mime_type", "") or ""
                is_video_doc = mime.startswith("video/")

            if not (is_video or is_video_doc):
                s.skipped_nonvid += 1
                s.processed += 1
                await _update_progress()
                continue

            file = message.video or message.document
            title = message.caption or file.file_name
            msg_id = message.id
            size = get_readable_file_size(file.file_size)
            channel = str(chat_id).replace("-100", "")
            channel_int = int(channel)

            # ── Duplicate check ──────────────────────────────────
            try:
                if await _stream_id_exists(channel_int, msg_id):
                    s.skipped_dup += 1
                    s.processed += 1
                    await _update_progress()
                    continue
            except Exception as e:
                LOGGER.warning(f"[Scanner] Dup-check error msg {msg_id}: {e}")

            # ── Metadata extraction (same pipeline as receiver) ──
            try:
                metadata_info = await metadata(clean_filename(title), channel_int, msg_id)
            except Exception as e:
                LOGGER.warning(f"[Scanner] Metadata exception for msg {msg_id}: {e}")
                metadata_info = None

            if metadata_info is None:
                s.skipped_meta += 1
                s.processed += 1
                await _update_progress()
                continue

            title_clean = remove_urls(title)
            if not title_clean.endswith(('.mkv', '.mp4')):
                title_clean += '.mkv'

            # ── Insert into DB ───────────────────────────────────
            try:
                updated_id = await db.insert_media(
                    metadata_info,
                    channel=channel_int,
                    msg_id=msg_id,
                    size=size,
                    name=title_clean,
                )
                if updated_id:
                    s.indexed += 1
                    LOGGER.info(f"[Scanner] Indexed msg {msg_id}: {title_clean}")
                else:
                    s.skipped_meta += 1
            except Exception as e:
                LOGGER.error(f"[Scanner] DB insert error msg {msg_id}: {e}")
                s.errors += 1

            s.processed += 1
            await _update_progress()

        # Track empty batches to know when to stop
        if batch_had_content:
            empty_streak = 0
        else:
            empty_streak += 1

        current += BATCH_SIZE
        # Small delay between batches
        await asyncio.sleep(RATE_LIMIT_DELAY)

    LOGGER.info(f"[Scanner] Finished {s.channel_name}: scanned up to ID {current}, "
                f"{s.total_found} messages found, {s.indexed} indexed")


# ── Bot Commands ─────────────────────────────────────────────────────────

@Client.on_message(filters.command('scan') & filters.private & CustomFilters.owner, group=10)
async def scan_command(client: Client, message: Message):
    """Scan AUTH_CHANNELs for existing content. Skips already-indexed messages."""
    if scan_state.running:
        await message.reply_text(
            "⚠️ A scan is already running. Use /scanstatus to check progress, "
            "or /cancelscan to abort it.",
            quote=True,
        )
        return

    # Optional: /scan -1001234567890  to scan a single channel
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        target_channels = [args[1].strip()]
    else:
        target_channels = list(Telegram.AUTH_CHANNEL)

    if not target_channels:
        await message.reply_text("❌ No AUTH_CHANNELs configured.", quote=True)
        return

    scan_state.reset()
    scan_state.running = True
    scan_state.started_at = time.time()
    scan_state.status_msg = await message.reply_text(
        "📡 <b>Channel scan starting…</b>",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        for ch_id_str in target_channels:
            if scan_state.cancelled:
                break
            try:
                ch_id = int(ch_id_str)
            except ValueError:
                LOGGER.warning(f"[Scanner] Invalid channel ID: {ch_id_str}")
                continue

            await _scan_channel(client, ch_id)

        # Final summary
        s = scan_state
        status = "🛑 Cancelled" if s.cancelled else "✅ Complete"
        summary = (
            f"<blockquote>📡 <b>Scan {status}</b></blockquote>\n\n"
            f"⏱ Duration: <code>{s.elapsed}</code>\n"
            f"📨 Total messages seen: <code>{s.total_found}</code>\n"
            f"✅ Newly indexed: <code>{s.indexed}</code>\n"
            f"⏭ Skipped (already in DB): <code>{s.skipped_dup}</code>\n"
            f"⚠️ Skipped (metadata fail): <code>{s.skipped_meta}</code>\n"
            f"📎 Skipped (non-video): <code>{s.skipped_nonvid}</code>\n"
            f"❌ Errors: <code>{s.errors}</code>"
        )
        try:
            await s.status_msg.edit_text(summary, parse_mode=enums.ParseMode.HTML)
        except Exception:
            await message.reply_text(summary, parse_mode=enums.ParseMode.HTML)

        # Send a separate notification so the user gets a Telegram ping
        # (editing a message doesn't trigger a notification)
        if s.processed > 20:
            notify = (
                f"{'🛑 Scan cancelled' if s.cancelled else '✅ Scan complete'} — "
                f"{s.indexed} indexed, {s.skipped_dup} skipped, "
                f"{s.errors} errors ({s.elapsed})"
            )
            await message.reply_text(notify)

    except (ChannelPrivate, ChatAdminRequired) as e:
        await message.reply_text(
            f"❌ <b>Access denied</b> to channel.\n\n"
            f"Make sure the bot is an admin in the channel.\n"
            f"<code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        LOGGER.error(f"[Scanner] Unexpected error: {e}")
        await message.reply_text(f"❌ Scan failed: <code>{e}</code>",
                                  parse_mode=enums.ParseMode.HTML)
    finally:
        scan_state.running = False


@Client.on_message(filters.command('rescan') & filters.private & CustomFilters.owner, group=10)
async def rescan_command(client: Client, message: Message):
    """Wipe all DB entries that belong to AUTH_CHANNELs, then do a full scan.

    This is the nuclear option — it re-indexes everything from scratch.
    """
    if scan_state.running:
        await message.reply_text(
            "⚠️ A scan is already running. Cancel it first with /cancelscan.",
            quote=True,
        )
        return

    channels = list(Telegram.AUTH_CHANNEL)
    if not channels:
        await message.reply_text("❌ No AUTH_CHANNELs configured.", quote=True)
        return

    confirm_msg = await message.reply_text(
        "⚠️ <b>RESCAN</b> will <u>delete all existing DB entries</u> "
        "for your AUTH_CHANNELs and re-index from scratch.\n\n"
        "Send <code>/rescan confirm</code> to proceed.",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )

    args = message.text.split()
    if len(args) < 2 or args[1].lower() != "confirm":
        return

    # Purge existing entries for these channels
    purge_msg = await message.reply_text(
        "🗑 Purging existing entries…", parse_mode=enums.ParseMode.HTML
    )
    purged = 0
    for ch_id_str in channels:
        channel_int = int(ch_id_str.replace("-100", ""))
        purged += await _purge_channel_entries(channel_int)

    await purge_msg.edit_text(
        f"🗑 Purged <code>{purged}</code> stream entries. Starting full scan…",
        parse_mode=enums.ParseMode.HTML,
    )

    # Now run a normal scan
    message.text = "/scan"  # trick to reuse scan_command logic
    await scan_command(client, message)


async def _purge_channel_entries(channel_int: int) -> int:
    """Delete all movie/tv stream entries whose encoded chat_id matches this channel."""
    from Backend.helper.encrypt import decode_string

    purged = 0
    for i in range(1, db.current_db_index + 1):
        storage = db.dbs.get(f"storage_{i}")
        if storage is None:
            continue

        # Purge movies
        async for movie in storage["movie"].find({}):
            remaining = []
            changed = False
            for q in movie.get("telegram", []):
                try:
                    decoded = await decode_string(q["id"])
                    if int(decoded["chat_id"]) == channel_int:
                        purged += 1
                        changed = True
                        continue
                except Exception:
                    pass
                remaining.append(q)
            if changed:
                if remaining:
                    movie["telegram"] = remaining
                    await storage["movie"].replace_one({"_id": movie["_id"]}, movie)
                else:
                    await storage["movie"].delete_one({"_id": movie["_id"]})

        # Purge TV
        async for tv in storage["tv"].find({}):
            tv_changed = False
            for season in tv.get("seasons", []):
                for episode in season.get("episodes", []):
                    remaining = []
                    for q in episode.get("telegram", []):
                        try:
                            decoded = await decode_string(q["id"])
                            if int(decoded["chat_id"]) == channel_int:
                                purged += 1
                                tv_changed = True
                                continue
                        except Exception:
                            pass
                        remaining.append(q)
                    episode["telegram"] = remaining
                # Remove empty episodes
                season["episodes"] = [
                    ep for ep in season["episodes"] if ep.get("telegram")
                ]
            # Remove empty seasons
            tv["seasons"] = [s for s in tv["seasons"] if s.get("episodes")]
            if tv_changed:
                if tv["seasons"]:
                    await storage["tv"].replace_one({"_id": tv["_id"]}, tv)
                else:
                    await storage["tv"].delete_one({"_id": tv["_id"]})

    return purged


@Client.on_message(filters.command('scanstatus') & filters.private & CustomFilters.owner, group=10)
async def scan_status_command(client: Client, message: Message):
    """Show current scan progress."""
    s = scan_state
    if not s.running:
        await message.reply_text("ℹ️ No scan is currently running.", quote=True)
        return

    text = (
        f"<blockquote>📡 <b>Scan in progress:</b> {s.channel_name}</blockquote>\n\n"
        f"⏱ Elapsed: <code>{s.elapsed}</code>\n"
        f"📨 Processed: <code>{s.processed}</code>\n"
        f"✅ Indexed: <code>{s.indexed}</code>\n"
        f"⏭ Skipped (duplicate): <code>{s.skipped_dup}</code>\n"
        f"⚠️ Skipped (metadata fail): <code>{s.skipped_meta}</code>\n"
        f"📎 Skipped (non-video): <code>{s.skipped_nonvid}</code>\n"
        f"❌ Errors: <code>{s.errors}</code>"
    )
    await message.reply_text(text, quote=True, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command('cancelscan') & filters.private & CustomFilters.owner, group=10)
async def cancel_scan_command(client: Client, message: Message):
    """Abort a running scan gracefully."""
    if not scan_state.running:
        await message.reply_text("ℹ️ No scan is currently running.", quote=True)
        return

    scan_state.cancelled = True
    await message.reply_text(
        "🛑 Scan cancellation requested. Will stop after current message.",
        quote=True,
    )
