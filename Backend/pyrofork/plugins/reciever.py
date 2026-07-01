from asyncio import Lock, Queue, create_task
from asyncio import sleep as asleep

from pyrogram import Client, filters
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import Message

import Backend
from Backend import db
from Backend.helper.auto_catalog import start_single_media_catalog_sync
from Backend.helper.metadata import extract_default_id, metadata
from Backend.helper.pyro import clean_filename, get_readable_file_size, remove_urls
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.split_files import parse_split_info, strip_part_suffix
from Backend.helper.task_manager import edit_message
from Backend.logger import LOGGER

file_queue = Queue()
db_lock = Lock()


#----- True when the message carries a streamable video or a split-archive part
def _is_supported_media(message: Message) -> bool:
    if message.video:
        return True
    if message.document:
        mime_type = message.document.mime_type or ""
        if mime_type.startswith("video/"):
            return True
        candidate = message.caption or message.document.file_name or ""
        if parse_split_info(candidate):
            return True
    return False


#----- Common message field extraction shared by the channel handlers
def _extract_fields(message: Message):
    file = message.video or message.document
    title = message.caption or file.file_name
    channel = str(message.chat.id).replace("-100", "")
    return file, title, message.id, file.file_size, get_readable_file_size(file.file_size), channel


#----- Strip URLs/part suffix from a title and ensure a video extension
def _finalize_title(title: str, metadata_info: dict) -> str:
    title = remove_urls(title)
    if metadata_info.get('group_key'):
        title = strip_part_suffix(title)
    if not title.endswith(('.mkv', '.mp4')):
        title += '.mkv'
    return title


#----- Serialize DB inserts from the queue and trigger catalog sync
async def process_file():
    while True:
        metadata_info, channel, msg_id, size, raw_size, title = await file_queue.get()
        async with db_lock:
            updated_id = await db.insert_media(metadata_info, channel=channel, msg_id=msg_id, size=size, raw_size=raw_size, name=title)
            if updated_id:
                LOGGER.info(f"{metadata_info['media_type']} updated with ID: {updated_id}")
            else:
                LOGGER.info("Update failed due to validation errors.")
        if updated_id:
            start_single_media_catalog_sync(
                db,
                tmdb_id=metadata_info.get("tmdb_id"),
                media_type=metadata_info.get("media_type"),
            )
        file_queue.task_done()


create_task(process_file())


#----- Ingest new channel media into the queue after building metadata
@Client.on_message(filters.channel & (filters.document | filters.video))
async def file_receive_handler(client: Client, message: Message):
    if str(message.chat.id) not in SettingsManager.current().auth_channels:
        await message.reply_text("> Channel is not in AUTH_CHANNEL")
        return
    try:
        if not _is_supported_media(message):
            await message.reply_text("> Not supported")
            return

        _, title, msg_id, raw_size, size, channel = _extract_fields(message)

        metadata_info = await metadata(clean_filename(title), int(channel), msg_id)
        if metadata_info is None:
            LOGGER.warning(f"Metadata failed for file: {title} (ID: {msg_id})")
            return

        title = _finalize_title(title, metadata_info)

        if Backend.USE_DEFAULT_ID:
            new_caption = (message.caption + "\n\n" + Backend.USE_DEFAULT_ID) if message.caption else Backend.USE_DEFAULT_ID
            create_task(edit_message(chat_id=message.chat.id, msg_id=message.id, new_caption=new_caption))

        await file_queue.put((metadata_info, int(channel), msg_id, size, raw_size, title))
    except FloodWait as e:
        LOGGER.info(f"Sleeping for {str(e.value)}s")
        await asleep(e.value)
        await message.reply_text(
            text=f"Got Floodwait of {str(e.value)}s",
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN
        )


#----- Re-index an edited channel file only when it carries an override ID
@Client.on_edited_message(filters.channel & (filters.document | filters.video))
async def file_edited_handler(client: Client, message: Message):
    if str(message.chat.id) not in SettingsManager.current().auth_channels:
        return
    try:
        if not _is_supported_media(message):
            return

        _, title, msg_id, raw_size, size, channel = _extract_fields(message)
        override_id = extract_default_id(message.caption) if message.caption else None
        if not override_id:
            return

        LOGGER.info(f"Detected override ID '{override_id}' in edited message {msg_id}")
        await db.remove_media_part(int(channel), msg_id)

        metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id)
        if metadata_info is None:
            LOGGER.warning(f"Metadata failed for edited file: {title} (ID: {msg_id})")
            return

        title = _finalize_title(title, metadata_info)
        await file_queue.put((metadata_info, int(channel), msg_id, size, raw_size, title))
    except Exception as e:
        LOGGER.error(f"Error handling edited generic file {message.id}: {e}")


#----- Purge database entries for messages deleted from auth channels
@Client.on_deleted_messages(filters.channel)
async def file_deleted_handler(client: Client, messages: list[Message]):
    try:
        for message in messages:
            if not (message.chat and str(message.chat.id) in SettingsManager.current().auth_channels):
                continue
            channel = str(message.chat.id).replace("-100", "")
            msg_id = message.id
            try:
                if await db.remove_media_part(int(channel), msg_id):
                    LOGGER.info(f"Automatically purged deleted message {msg_id} from database.")
            except Exception as ex:
                LOGGER.error(f"Failed to scrub deleted message {msg_id}: {ex}")
    except Exception as e:
        LOGGER.error(f"Error handling deleted messages: {e}")
