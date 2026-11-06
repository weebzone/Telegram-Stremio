"""Files that could not be indexed: record them, let the dashboard fix and retry them."""
import asyncio
import time

from Backend import db
from Backend.helper.encrypt import encode_string
from Backend.helper.manual_add import quality_from_height
from Backend.helper.metadata import caption_with_links, extract_default_id, metadata
from Backend.helper.metadata.entry import _is_anime_channel
from Backend.helper.metadata.parse import analyze_metadata_failure, parse_media_name
from Backend.helper.pyro import apply_video_thumb_to_metadata, clean_filename, finalize_media_name, get_readable_file_size
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

_retry_lock = asyncio.Lock()
import_state = {"running": False, "scanned": 0, "found": 0, "added": 0, "error": ""}


#----- Plain dict describing a message the indexer gave up on (no I/O, easy to test)
def build_doc(message, reason: str = None, anime: bool = None) -> dict:
    media = getattr(message, "video", None) or getattr(message, "document", None)
    chat_id = str(message.chat.id).replace("-100", "")
    file_name = getattr(media, "file_name", None) or ""
    caption = getattr(message, "caption", None) or ""
    return {
        "_id": db.skipped_id(chat_id, message.id),
        "chat_id": chat_id,
        "msg_id": message.id,
        "file_name": file_name,
        "caption": caption,
        "raw_size": getattr(media, "file_size", 0) or 0,
        "height": getattr(media, "height", 0) or 0,
        "anime": _is_anime_channel(chat_id) if anime is None else anime,
        "reason": reason or analyze_metadata_failure(clean_filename(caption or file_name)),
        "created_at": time.time(),
    }


async def record_skipped(message) -> None:
    try:
        await db.upsert_skipped_file(build_doc(message))
    except Exception as e:
        LOGGER.warning(f"[Skipped] Could not record message {getattr(message, 'id', '?')}: {e}")


#----- The original upload was deleted and only the skip-channel copy remains: point the record at the copy
async def retarget_to_copy(original, copied) -> None:
    try:
        old = db.skipped_id(str(original.chat.id).replace("-100", ""), original.id)
        old_doc = await db.get_skipped_file(old) or {}
        doc = build_doc(copied, reason=old_doc.get("reason"), anime=old_doc.get("anime"))
        doc["caption"] = doc["caption"] or old_doc.get("caption", "")
        await db.upsert_skipped_file(doc)
        await db.delete_skipped_file(old)
    except Exception as e:
        LOGGER.warning(f"[Skipped] Could not retarget record to the skip-channel copy: {e}")


#----- Re-run indexing for one skipped file, optionally with an IMDb/TMDB id, title or season from the admin.
#----- A typed title replaces the whole file name, so it should include season/episode for series.
#----- Returns {"ok": bool, "message": str}; on failure the stored reason is refreshed.
async def retry_skipped(client, skipped_id: str, id_override: str = None, title: str = None, season: int = None) -> dict:
    doc = await db.get_skipped_file(skipped_id)
    if not doc:
        return {"ok": False, "message": "Not found."}

    channel = int(doc["chat_id"])
    message = await client.get_messages(int(f"-100{channel}"), int(doc["msg_id"]))
    media = (getattr(message, "video", None) or getattr(message, "document", None)) if message else None
    if not media or getattr(message, "empty", False):
        await db.set_skipped_file(skipped_id, status="missing", reason="The message no longer exists or has no file.")
        return {"ok": False, "message": "The Telegram message is gone."}

    original = message.caption or media.file_name or ""
    source = (title or "").strip() or original
    override = (id_override or "").strip() or extract_default_id(caption_with_links(message))

    #----- Quality: real video height, else whatever the original name says, else a plain "HD" label
    try:
        name_quality = parse_media_name(clean_filename(original)).get("quality")
    except Exception:
        name_quality = None
    fallback_quality = quality_from_height(getattr(media, "height", 0) or 0) or name_quality or "HD"

    info = await metadata(
        clean_filename(source), channel, message.id,
        override_id=override or None,
        season_hint=season,
        fallback_quality=fallback_quality,
        force_anime=bool(doc.get("anime")),
    )
    if info is None:
        reason = analyze_metadata_failure(clean_filename(source))
        await db.set_skipped_file(skipped_id, reason=reason, attempts=(doc.get("attempts") or 0) + 1)
        return {"ok": False, "message": reason}

    encoded = info.get("encoded_string") or await encode_string({"chat_id": channel, "msg_id": message.id})
    await apply_video_thumb_to_metadata(info, message, encoded, client)
    name = finalize_media_name(source, bool(info.get("group_key")))
    async with _retry_lock:
        done = await db.insert_media(
            info, channel=channel, msg_id=message.id,
            size=get_readable_file_size(media.file_size), raw_size=media.file_size, name=name,
        )
    if not done:
        return {"ok": False, "message": "The database rejected the entry (validation or duplicate)."}

    try:  # same follow-ups as a fresh upload
        from Backend.helper.auto_catalog import start_single_media_catalog_sync
        start_single_media_catalog_sync(db, tmdb_id=info.get("tmdb_id"), media_type=info.get("media_type"))
    except Exception as e:
        LOGGER.warning(f"[Skipped] Catalog sync after retry failed: {e}")
    return {"ok": True, "message": f"Indexed as {info.get('title') or name}."}


#----- Pull files already sitting in the skip channel into the list (they were skipped before this feature existed)
async def import_skip_channel(client, batch: int = 200, stop_after_empty: int = 5) -> None:
    st = import_state
    st.update(running=True, scanned=0, found=0, added=0, error="")
    try:
        skip = SettingsManager.current().skip_channel
        if not skip:
            st["error"] = "No skip channel is set in Settings."
            return
        chat = int(skip) if str(skip).lstrip("-").isdigit() else skip
        first, empty_run = 1, 0
        while empty_run < stop_after_empty:
            msgs = await client.get_messages(chat, list(range(first, first + batch)))
            real = [m for m in msgs if not getattr(m, "empty", False)]
            empty_run = 0 if real else empty_run + 1
            for m in real:
                if not (getattr(m, "video", None) or getattr(m, "document", None)):
                    continue
                st["found"] += 1
                channel_int = int(str(m.chat.id).replace("-100", ""))
                if await db.stream_exists(channel_int, m.id):
                    continue            # already indexed (retried earlier)
                if await db.get_skipped_file(db.skipped_id(channel_int, m.id)):
                    continue
                await db.upsert_skipped_file(build_doc(m))
                st["added"] += 1
            st["scanned"] += batch
            first += batch
            await asyncio.sleep(0.5)
    except Exception as e:
        LOGGER.warning(f"[Skipped] Import from skip channel failed: {e}")
        st["error"] = str(e)
    finally:
        st["running"] = False
