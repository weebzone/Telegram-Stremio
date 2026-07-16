from asyncio import Lock, Queue, create_task
from asyncio import sleep as asleep

from pyrogram import Client, filters
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import Message

import Backend
from Backend import db
from Backend.helper.announcer import announce_new_media
from Backend.helper.auto_catalog import start_single_media_catalog_sync
from Backend.helper.encrypt import encode_string
from Backend.helper.manual_add import resolve_telegram_message, stamp_caption_with_id
from Backend.helper.requests_manager import auto_fulfill
from Backend.helper.metadata import analyze_metadata_failure, extract_default_id, metadata
from Backend.helper.pyro import clean_filename, finalize_media_name, get_readable_file_size
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.split_files import parse_split_info
from Backend.helper.subtitles import ingest_subtitle, is_subtitle_file, remove_subtitle
from Backend.logger import LOGGER

file_queue = Queue()
db_lock = Lock()
manual_session_lock = Lock()


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


#----- True when a chat id belongs to a manual channel (files added by hand, not auto-indexed)
def _is_manual_channel(chat_id) -> bool:
    target = str(chat_id).replace("-100", "")
    return any(str(c).strip().replace("-100", "") == target for c in SettingsManager.current().manual_channels)


#----- True when a message belongs to the skip channel (never indexed)
def _is_skip_channel(message: Message) -> bool:
    skip = SettingsManager.current().skip_channel
    if not skip:
        return False
    ref = str(skip).strip()
    if ref.lstrip("@-").replace("-100", "").isdigit():
        return ref.replace("-100", "").lstrip("@") == str(message.chat.id).replace("-100", "")
    username = (getattr(message.chat, "username", None) or "").lower()
    return bool(username) and ref.lstrip("@").lower() == username


async def _route_to_skip_channel(client: Client, message: Message, reason: str) -> None:
    settings = SettingsManager.current()
    skip = settings.skip_channel
    if not skip:
        return

    skip_chat = int(skip) if str(skip).lstrip("-").replace("-100", "").isdigit() else skip

    try:
        copied = await message.copy(skip_chat)
    except FloodWait as e:
        await asleep(e.value)
        try:
            copied = await message.copy(skip_chat)
        except Exception as e2:
            LOGGER.error(f"[SkipChannel] Copy failed for message {message.id}: {e2}")
            return
    except Exception as e:
        LOGGER.error(f"[SkipChannel] Could not copy message {message.id} to skip channel: {e}")
        return

    note = (
        "⚠️ Not indexed — metadata check failed\n\n"
        f"Reason: {reason}\n\n"
        "Fix the caption (a clear title, a quality like 1080p, or an IMDb / TMDB link or id) and "
        "forward it to the main channel again, or add it manually from the panel."
    )
    try:
        await client.send_message(skip_chat, note, reply_to_message_id=copied.id, disable_web_page_preview=True)
    except Exception as e:
        LOGGER.warning(f"[SkipChannel] Could not post note for message {message.id}: {e}")

    if settings.delete_on_metadata_fail:
        try:
            from Backend.helper.task_manager import delete_message
            await delete_message(message.chat.id, message.id)
        except Exception as e:
            LOGGER.warning(f"[SkipChannel] Could not delete original message {message.id}: {e}")


#----- Common message field extraction shared by the channel handlers
def _extract_fields(message: Message):
    file = message.video or message.document
    title = message.caption or file.file_name
    channel = str(message.chat.id).replace("-100", "")
    return file, title, message.id, file.file_size, get_readable_file_size(file.file_size), channel


#----- Strip URLs/part suffix from a title and ensure a video extension
def _finalize_title(title: str, metadata_info: dict) -> str:
    return finalize_media_name(title, bool(metadata_info.get('group_key')))


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
            announce_new_media(metadata_info)
            create_task(auto_fulfill(
                tmdb_id=metadata_info.get("tmdb_id"),
                imdb_id=metadata_info.get("imdb_id"),
                media_type=metadata_info.get("media_type"),
            ))
        file_queue.task_done()


create_task(process_file())


#----- Build a title-level metadata base from an existing media document
def _base_from_doc(doc: dict) -> dict:
    return {
        "tmdb_id": doc.get("tmdb_id"),
        "imdb_id": doc.get("imdb_id"),
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or 0,
        "rate": doc.get("rating") or 0,
        "description": doc.get("description") or "",
        "poster": doc.get("poster") or "",
        "backdrop": doc.get("backdrop") or "",
        "logo": doc.get("logo") or "",
        "genres": doc.get("genres") or [],
        "cast": doc.get("cast") or [],
        "runtime": str(doc.get("runtime") or ""),
        "original_language": doc.get("original_language"),
        "origin_country": doc.get("origin_country") or [],
    }


#----- Highest existing episode number in a season, or 0 if none
def _max_episode(doc: dict, season_number: int) -> int:
    for season in doc.get("seasons", []) or []:
        if season.get("season_number") == season_number:
            eps = [e.get("episode_number", 0) for e in season.get("episodes", []) or []]
            return max(eps) if eps else 0
    return 0


async def _handle_personal_session(client: Client, message: Message) -> None:
    session = Backend.MANUAL_SESSION
    if not session:
        return
    async with manual_session_lock:
        channel = str(message.chat.id).replace("-100", "")
        try:
            resolved = await resolve_telegram_message(client, chat_id=channel, msg_id=message.id)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] Could not resolve message {message.id}: {e}")
            return

        tmdb_id = session["tmdb_id"]
        media_type = session["media_type"]
        location = await db.find_media_doc(media_type, tmdb_id)
        if not location:
            LOGGER.warning(f"[Manual Session] Target id {tmdb_id} not found; ignoring file.")
            return
        doc = location[0]

        p_channel = int(resolved["chat_id"])
        p_msg = int(resolved["msg_id"])
        encoded = await encode_string({"chat_id": p_channel, "msg_id": p_msg})
        name = resolved["name"]
        quality = resolved.get("quality") or session.get("quality") or "HD"

        split_key = resolved.get("split_key")
        metadata_info = _base_from_doc(doc)
        metadata_info.update({
            "media_type": media_type,
            "quality": quality,
            "encoded_string": encoded,
            "group_key": f"{channel}:{quality}:{split_key}" if split_key else None,
            "part_number": resolved.get("part_number"),
            "is_anime": bool(doc.get("is_anime")),
        })

        if media_type == "tv":
            season_number = session["season"]
            episode_number = session["episode"]
            if episode_number is None:
                episode_number = _max_episode(doc, season_number) + 1
            thumb_url = ""
            if resolved.get("has_thumb"):
                thumb_url = f"/thumb/{encoded}"
            metadata_info.update({
                "season_number": season_number,
                "episode_number": episode_number,
                "episode_title": f"S{season_number:02d}E{episode_number:02d}",
                "episode_backdrop": thumb_url or metadata_info.get("backdrop") or "",
                "episode_overview": "",
                "episode_released": "",
            })

        async with db_lock:
            updated_id = await db.insert_media(
                metadata_info, channel=p_channel, msg_id=p_msg,
                size=resolved["size"], name=name, raw_size=int(resolved.get("raw_size") or 0),
            )

        if updated_id:
            where = (f"S{metadata_info['season_number']:02d}E{metadata_info['episode_number']:02d} "
                     if media_type == "tv" else "")
            LOGGER.info(f"[Manual Session] Added {quality} {where}to '{metadata_info.get('title')}' (id {tmdb_id}).")
            create_task(stamp_caption_with_id(message, metadata_info))
        else:
            LOGGER.warning(f"[Manual Session] Insert failed for message {message.id}.")


#----- Ingest new channel media into the queue after building metadata
@Client.on_message(filters.channel & (filters.document | filters.video))
async def file_receive_handler(client: Client, message: Message):
    if _is_skip_channel(message):
        return

    session = Backend.MANUAL_SESSION
    is_manual = _is_manual_channel(message.chat.id)

    #----- Manual channel + personal session: add straight onto the personal title
    if is_manual and session and session.get("kind") == "personal":
        await _handle_personal_session(client, message)
        return

    #----- Manual channel otherwise only proceeds during a real (TMDB/IMDb) session;
    #----- real files are parsed from their name/caption and forced onto the session id.
    if is_manual:
        if not (session and session.get("kind") == "real"):
            return
    elif str(message.chat.id) not in SettingsManager.current().auth_channels:
        await message.reply_text("> Channel is not in AUTH_CHANNEL")
        return

    is_real_session = bool(session and session.get("kind") == "real")
    override_id = session["default_id"] if is_real_session else None
    season_hint = session.get("season") if is_real_session else None
    try:
        sub_name = (message.document.file_name if message.document else "") or ""
        if sub_name and is_subtitle_file(sub_name):
            channel = str(message.chat.id).replace("-100", "")
            create_task(ingest_subtitle(sub_name, int(channel), message.id))
            return

        if not _is_supported_media(message):
            await message.reply_text("> Not supported")
            return

        _, title, msg_id, raw_size, size, channel = _extract_fields(message)

        metadata_info = await metadata(clean_filename(title), int(channel), msg_id, override_id=override_id, season_hint=season_hint)
        if metadata_info is None:
            LOGGER.warning(f"Metadata failed for file: {title} (ID: {msg_id})")
            reason = analyze_metadata_failure(clean_filename(title))
            await _route_to_skip_channel(client, message, reason)
            return

        title = _finalize_title(title, metadata_info)

        await file_queue.put((metadata_info, int(channel), msg_id, size, raw_size, title))

        if is_real_session:
            create_task(stamp_caption_with_id(message, metadata_info))
    except FloodWait as e:
        LOGGER.info(f"Sleeping for {str(e.value)}s")
        await asleep(e.value)
        await message.reply_text(
            text=f"Got Floodwait of {str(e.value)}s",
            disable_web_page_preview=True,
            parse_mode=ParseMode.MARKDOWN
        )


def _override_matches_indexed(override_id: str, imdb_id, tmdb_id) -> bool:
    oid = str(override_id).strip().lower()
    if oid.startswith("tt"):
        return bool(imdb_id) and oid == str(imdb_id).strip().lower()
    if oid.isdigit():
        return tmdb_id not in (None, "") and oid == str(tmdb_id).strip()
    return False


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

        existing_ids = await db.get_media_ids_by_part(int(channel), msg_id)
        if existing_ids and _override_matches_indexed(override_id, existing_ids[0], existing_ids[1]):
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
            if not message.chat:
                continue
            if not (str(message.chat.id) in SettingsManager.current().auth_channels or _is_manual_channel(message.chat.id)):
                continue
            channel = str(message.chat.id).replace("-100", "")
            msg_id = message.id
            try:
                if await db.remove_media_part(int(channel), msg_id):
                    LOGGER.info(f"Automatically purged deleted message {msg_id} from database.")
                if await remove_subtitle(int(channel), msg_id):
                    LOGGER.info(f"Automatically purged deleted subtitle {msg_id} from database.")
            except Exception as ex:
                LOGGER.error(f"Failed to scrub deleted message {msg_id}: {ex}")
    except Exception as e:
        LOGGER.error(f"Error handling deleted messages: {e}")
