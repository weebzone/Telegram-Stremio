from asyncio import Queue, Lock, create_task, sleep as asleep

import Backend
from pyrogram import Client, filters
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from Backend import db
from Backend.config import Telegram
from Backend.helper.encrypt import encode_string
from Backend.helper.metadata import metadata, extract_default_id
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.task_manager import edit_message
from Backend.helper.torrent_source import (
    TorrentItem,
    extract_magnet_links,
    parse_magnet,
    parse_torrent,
)
from Backend.logger import LOGGER
from Backend.helper.disk_cache import PRECACHE_MANAGER, PrecacheJob


file_queue = Queue()
db_lock = Lock()
reply_queue = Queue()

METADATA_FAILED_TEXT = (
    "Metadata failed for this file. The filename may be valid, but the external metadata service "
    "may be temporarily unavailable. Try again after a minute, or add an IMDb/TMDb link in the caption. "
    "For TV files, use S01E01 for episodes or S01 COMBINED for a full-season pack."
)

TORRENT_METADATA_FAILED_TEXT = (
    "Metadata failed for this torrent. Add an IMDb/TMDb link, or use a clearer title like "
    "Movie Name 2024 1080p / Show.Name.S01E01. For full-season torrents, episode filenames "
    "should include S01E01 style numbering."
)

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".webm", ".mov", ".flv", ".wmv")


async def process_file():
    while True:
        metadata_info, channel, msg_id, size, title, chat_id, original_msg_id = await file_queue.get()
        async with db_lock:
            updated_id = await db.insert_media(
                metadata_info,
                channel=channel,
                msg_id=msg_id,
                size=size,
                name=title,
            )
            if updated_id:
                LOGGER.info(f"{metadata_info['media_type']} updated with ID: {updated_id}")
                await reply_queue.put((chat_id, original_msg_id, metadata_info, title, size))
            else:
                LOGGER.info("Update failed due to validation errors.")
        file_queue.task_done()


async def send_reply_messages():
    from Backend.pyrofork.bot import StreamBot

    while True:
        chat_id, msg_id, metadata_info, title, size = await reply_queue.get()
        try:
            base_url = Telegram.BASE_URL.rstrip("/")
            token = Telegram.DEFAULT_ADDON_TOKEN
            imdb_id = metadata_info.get("imdb_id", "")
            media_type = metadata_info.get("media_type", "movie")
            movie_title = metadata_info.get("title", "Unknown")
            year = metadata_info.get("year", "")
            quality = metadata_info.get("quality", "")
            encoded_string = metadata_info.get("encoded_string", "")
            rating = metadata_info.get("rate", "")
            source_type = metadata_info.get("source_type", "telegram")

            if imdb_id:
                if media_type == "tv":
                    season = metadata_info.get("season_number", 1)
                    episode = metadata_info.get("episode_number", 1)
                    stremio_link = f"{base_url}/stremio/open/series/{imdb_id}?season={season}&episode={episode}"
                else:
                    stremio_link = f"{base_url}/stremio/open/movie/{imdb_id}"
            else:
                stremio_link = f"{base_url}/stremio/{token}/configure" if token else f"{base_url}/stremio"

            if source_type == "telegram" and encoded_string and token:
                direct_stream = f"{base_url}/dl/{token}/{encoded_string}/video.mkv"
                vlc_page = f"{base_url}/vlc/{token}/{encoded_string}"
            else:
                direct_stream = "N/A"
                vlc_page = None

            rating_str = f"⭐ {rating}" if rating else ""
            if source_type == "torrent":
                help_note = "🧲 Torrent stream. Playback speed depends on seeders/peers.\n\n"
                stream_note = ""
            else:
                help_note = (
                    "⚠️ If streaming is slow or not loading, turn on Cloudflare WARP and try again.\n"
                    "Facing any issues? Type it here itself.\n\n"
                )
                stream_note = f"▶️ <b>Direct Stream Link:</b>\n<code>{direct_stream}</code>"
            if media_type == "tv":
                season = int(metadata_info.get("season_number", 0) or 0)
                episode = int(metadata_info.get("episode_number", 0) or 0)
                ep_title = metadata_info.get("episode_title", "")
                if metadata_info.get("season_pack"):
                    episode_count = int(metadata_info.get("season_pack_episode_count", 0) or 0)
                    reply_text = (
                        f"🎬 <b>{movie_title}</b>"
                        f"{f' ({year})' if year else ''}\n"
                        f"📺 Season {season:02d} Pack"
                        f"{f' | {episode_count} episodes' if episode_count else ''}\n"
                        f"{rating_str}"
                        f"{f' | {quality}' if quality else ''}"
                        f" | {size}\n\n"
                        f"{help_note}"
                        f"{stream_note}"
                    )
                else:
                    reply_text = (
                        f"🎬 <b>{movie_title}</b>"
                        f"{f' ({year})' if year else ''}\n"
                        f"📺 S{season:02d}E{episode:02d}"
                        f"{f' - {ep_title}' if ep_title else ''}\n"
                        f"{rating_str}"
                        f"{f' | {quality}' if quality else ''}"
                        f" | {size}\n\n"
                        f"{help_note}"
                        f"{stream_note}"
                    )
            else:
                reply_text = (
                    f"🎬 <b>{movie_title}</b>"
                    f"{f' ({year})' if year else ''}\n"
                    f"{rating_str}"
                    f"{f' | {quality}' if quality else ''}"
                    f" | {size}\n\n"
                    f"{help_note}"
                    f"{stream_note}"
                )

            buttons_row = [InlineKeyboardButton("▶️ Watch in Stremio", url=stremio_link)]
            if vlc_page:
                buttons_row.append(InlineKeyboardButton("🎬 Watch in VLC", url=vlc_page))
            buttons = InlineKeyboardMarkup([buttons_row])

            await StreamBot.send_message(
                chat_id=chat_id,
                text=reply_text,
                reply_to_message_id=msg_id,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=buttons,
            )
            LOGGER.info(f"Sent stream link reply for: {movie_title}")
        except FloodWait as e:
            LOGGER.info(f"FloodWait in reply: sleeping for {e.value}s")
            await asleep(e.value)
        except Exception as e:
            LOGGER.error(f"Failed to send reply message: {e}")
        reply_queue.task_done()


create_task(process_file())
create_task(send_reply_messages())


def _is_video_document(message: Message) -> bool:
    return bool(
        message.document and (
            (message.document.mime_type and message.document.mime_type.startswith("video/")) or
            (message.document.file_name and message.document.file_name.lower().endswith(VIDEO_EXTENSIONS))
        )
    )


def _is_torrent_document(message: Message) -> bool:
    return bool(
        message.document and (
            (message.document.file_name and message.document.file_name.lower().endswith(".torrent")) or
            message.document.mime_type == "application/x-bittorrent"
        )
    )


def _message_text(message: Message) -> str:
    return message.text or message.caption or ""


def _torrent_title(item: TorrentItem, text: str) -> str:
    title = item.file_name or item.display_name
    if title and title != item.info_hash:
        return title

    cleaned_text = text
    for magnet in extract_magnet_links(text):
        cleaned_text = cleaned_text.replace(magnet, " ")
    cleaned_text = remove_urls(cleaned_text).strip()
    return cleaned_text or item.display_name


async def _queue_torrent_item(message: Message, item: TorrentItem, override_id: str | None) -> bool:
    channel = int(str(message.chat.id).replace("-100", ""))
    msg_id = message.id
    raw_text = _message_text(message)
    title = _torrent_title(item, raw_text)
    if not title or title == item.info_hash:
        return False

    metadata_info = await metadata(clean_filename(title), channel, msg_id, override_id=override_id)
    if metadata_info is None:
        LOGGER.warning(f"Metadata failed for torrent item: {title} (ID: {msg_id})")
        return False

    encoded_string = await encode_string({
        "source_type": "torrent",
        "chat_id": channel,
        "msg_id": msg_id,
        "info_hash": item.info_hash,
        "file_idx": item.file_idx,
        "name": item.file_name,
    })
    display_title = remove_urls(item.file_name or title)
    if display_title and not display_title.lower().endswith(VIDEO_EXTENSIONS):
        display_title += ".mkv"

    metadata_info.update({
        "source_type": "torrent",
        "encoded_string": encoded_string,
        "info_hash": item.info_hash,
        "file_idx": item.file_idx,
        "sources": item.sources,
        "filename": item.file_name or display_title,
        "video_size": item.size_bytes,
        "origin_chat_id": int(message.chat.id),
        "origin_msg_id": int(msg_id),
        "torrent_private": bool(item.is_private),
    })
    await file_queue.put((
        metadata_info,
        channel,
        msg_id,
        item.size_text,
        display_title or title,
        message.chat.id,
        message.id,
    ))
    db.queue_torrent_stats_refresh(
        item.info_hash,
        item.sources,
        torrent_private=bool(item.is_private),
    )
    return True


async def _handle_torrent_message(client: Client, message: Message) -> bool:
    text = _message_text(message)
    override_id = extract_default_id(text) if text else None
    items: list[TorrentItem] = []

    for magnet in extract_magnet_links(text):
        try:
            items.append(parse_magnet(magnet, fallback_name=None))
        except Exception as e:
            LOGGER.warning(f"Failed to parse magnet in message {message.id}: {e}")

    if _is_torrent_document(message):
        try:
            torrent_file = await client.download_media(message, in_memory=True)
            torrent_file.seek(0)
            items.extend(parse_torrent(torrent_file.read()))
            torrent_file.close()
        except Exception as e:
            LOGGER.warning(f"Failed to parse torrent document {message.id}: {e}")

    if not items:
        return False

    queued = 0
    for item in items:
        if await _queue_torrent_item(message, item, override_id):
            queued += 1

    if queued == 0:
        await message.reply_text(TORRENT_METADATA_FAILED_TEXT)
        return True

    LOGGER.info(f"Queued {queued} torrent stream(s) from message {message.id}.")
    return True


@Client.on_message(filters.channel & (filters.document | filters.video | filters.text))
async def file_receive_handler(client: Client, message: Message):
    if str(message.chat.id) in Telegram.AUTH_CHANNEL:
        try:
            if await _handle_torrent_message(client, message):
                return

            is_video_doc = _is_video_document(message)
            if message.video or is_video_doc:
                file = message.video or message.document
                file_name = getattr(file, "file_name", None) or ""
                if file_name and file_name.lower().endswith(VIDEO_EXTENSIONS):
                    title = file_name
                else:
                    title = message.caption or file_name or "unknown"

                msg_id = message.id
                size = get_readable_file_size(file.file_size)
                channel = str(message.chat.id).replace("-100", "")

                # Background pre-cache to disk (optional, behind config flags)
                try:
                    if getattr(file, "file_unique_id", None) and getattr(file, "file_size", None):
                        create_task(
                            PRECACHE_MANAGER.enqueue(
                                client,
                                PrecacheJob(
                                    chat_id=int(message.chat.id),
                                    msg_id=int(msg_id),
                                    unique_id=str(file.file_unique_id),
                                    expected_size=int(file.file_size or 0),
                                ),
                            )
                        )
                except Exception as e:
                    LOGGER.debug(f"Precache enqueue failed for msg {msg_id}: {e}")

                metadata_info = await metadata(clean_filename(title), int(channel), msg_id)
                if metadata_info is None:
                    LOGGER.warning(f"Metadata failed for file: {title} (ID: {msg_id})")
                    await message.reply_text(METADATA_FAILED_TEXT)
                    return

                title = remove_urls(title)
                if not title.endswith((".mkv", ".mp4")):
                    title += ".mkv"

                if Backend.USE_DEFAULT_ID:
                    new_caption = (message.caption + "\n\n" + Backend.USE_DEFAULT_ID) if message.caption else Backend.USE_DEFAULT_ID
                    create_task(edit_message(chat_id=message.chat.id, msg_id=message.id, new_caption=new_caption))

                await file_queue.put((metadata_info, int(channel), msg_id, size, title, message.chat.id, message.id))
            else:
                await message.reply_text("> Not supported")
        except FloodWait as e:
            LOGGER.info(f"Sleeping for {str(e.value)}s")
            await asleep(e.value)
            await message.reply_text(
                text=f"Got Floodwait of {str(e.value)}s",
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await message.reply_text("> Channel is not in AUTH_CHANNEL")


@Client.on_edited_message(filters.channel & (filters.document | filters.video | filters.text))
async def file_edited_handler(client: Client, message: Message):
    if str(message.chat.id) in Telegram.AUTH_CHANNEL:
        try:
            if await _handle_torrent_message(client, message):
                return

            if message.video or _is_video_document(message):
                file = message.video or message.document
                title = message.caption or file.file_name
                msg_id = message.id
                size = get_readable_file_size(file.file_size)
                channel = str(message.chat.id).replace("-100", "")
                override_id = extract_default_id(message.caption) if message.caption else None

                # Pre-cache edited media too (in case the file itself changed)
                try:
                    if getattr(file, "file_unique_id", None) and getattr(file, "file_size", None):
                        create_task(
                            PRECACHE_MANAGER.enqueue(
                                client,
                                PrecacheJob(
                                    chat_id=int(message.chat.id),
                                    msg_id=int(msg_id),
                                    unique_id=str(file.file_unique_id),
                                    expected_size=int(file.file_size or 0),
                                ),
                            )
                        )
                except Exception as e:
                    LOGGER.debug(f"Precache enqueue failed for edited msg {msg_id}: {e}")

                if override_id:
                    LOGGER.info(f"Detected override ID '{override_id}' in edited message {msg_id}")
                    stream_id_hash = await encode_string({"chat_id": int(channel), "msg_id": msg_id})
                    await db.delete_media_by_stream_id(stream_id_hash)
                    metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)
                    if metadata_info is None:
                        LOGGER.warning(f"Metadata failed for edited file: {title} (ID: {msg_id})")
                        await message.reply_text(METADATA_FAILED_TEXT)
                        return
                    title = remove_urls(title)
                    if not title.endswith((".mkv", ".mp4")):
                        title += ".mkv"
                    await file_queue.put((metadata_info, int(channel), msg_id, size, title, message.chat.id, message.id))
        except Exception as e:
            LOGGER.error(f"Error handling edited generic file {message.id}: {e}")


@Client.on_deleted_messages(filters.channel)
async def file_deleted_handler(client: Client, messages: list[Message]):
    try:
        for message in messages:
            if message.chat and str(message.chat.id) in Telegram.AUTH_CHANNEL:
                channel = str(message.chat.id).replace("-100", "")
                msg_id = message.id
                try:
                    stream_id_hash = await encode_string({"chat_id": int(channel), "msg_id": msg_id})
                    deleted = await db.delete_media_by_origin(int(message.chat.id), int(msg_id))
                    if not deleted:
                        deleted = await db.delete_media_by_stream_id(stream_id_hash)
                    if deleted:
                        LOGGER.info(f"Automatically purged deleted message {msg_id} from database.")
                except Exception as ex:
                    LOGGER.error(f"Failed to scrub deleted message {msg_id}: {ex}")
    except Exception as e:
        LOGGER.error(f"Error handling deleted messages: {e}")
