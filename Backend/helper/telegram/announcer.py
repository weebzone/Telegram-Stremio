from asyncio import create_task
from datetime import datetime

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageDeleteForbidden, MessageIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Backend import db
from Backend.helper.settings.manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, get_streambot_url


#----- Accept either a numeric channel id (-100...) or an @username
def _resolve_chat(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


#----- Atomically claim a title so it is announced at most once; returns True if newly claimed
async def _claim(media_type: str, tmdb_id) -> bool:
    if not tmdb_id:
        return False
    result = await db.dbs["tracking"]["announced"].update_one(
        {"_id": f"{media_type}:{tmdb_id}"},
        {"$setOnInsert": {"at": datetime.utcnow()}},
        upsert=True,
    )
    return result.upserted_id is not None


async def _store_announcement_msg(media_type: str, tmdb_id, chat_id, message_id: int) -> None:
    if not tmdb_id or not message_id:
        return
    try:
        await db.dbs["tracking"]["announced"].update_one(
            {"_id": f"{media_type}:{tmdb_id}"},
            {"$set": {"chat_id": chat_id, "message_id": message_id, "at": datetime.utcnow()}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"Failed to store announcement message id: {e}")


def _build_caption(info: dict) -> str:
    is_tv = info.get("media_type") == "tv"
    title = info.get("title") or "Unknown"
    header = f"{'📺' if is_tv else '🎬'} <b>{title}</b>"
    if info.get("year"):
        header += f" ({info['year']})"

    lines = [header, "", f"🗂 <b>Type:</b> {'Series' if is_tv else 'Movie'}"]
    if info.get("rate"):
        try:
            lines.append(f"⭐ <b>Rating:</b> {round(float(info['rate']), 1)}")
        except (TypeError, ValueError):
            pass
    genres = info.get("genres") or []
    if genres:
        lines.append(f"🎭 <b>Genres:</b> {', '.join(genres[:4])}")
    if info.get("quality"):
        lines.append(f"📶 <b>Quality:</b> {info['quality']}")

    desc = (info.get("description") or "").strip()
    if desc:
        if len(desc) > 320:
            desc = desc[:317].rstrip() + "..."
        lines += ["", f"<i>{desc}</i>"]
    return "\n".join(lines)


def _build_markup(info: dict):
    rows = []
    base = SettingsManager.current().base_url
    imdb_id = str(info.get("imdb_id") or "").strip()
    stremio_type = "series" if info.get("media_type") == "tv" else "movie"
    if base and imdb_id:
        rows.append([
            InlineKeyboardButton("▶️ Stremio", url=f"{base}/open/stremio/{stremio_type}/{imdb_id}"),
            InlineKeyboardButton("📱 Nuvio", url=f"{base}/open/nuvio/{stremio_type}/{imdb_id}"),
        ])
    bot_url = get_streambot_url()
    if bot_url and bot_url != "https://t.me/":
        rows.append([InlineKeyboardButton("🤖 Get Addon", url=bot_url)])
    return InlineKeyboardMarkup(rows) if rows else None


async def _announce(info: dict) -> None:
    settings = SettingsManager.current()
    chat = _resolve_chat(settings.announcement_channel)
    if not settings.announce_new_content or chat is None:
        return
    if not await _claim(info.get("media_type"), info.get("tmdb_id")):
        return

    caption = _build_caption(info)
    poster = info.get("backdrop") or info.get("poster")
    markup = _build_markup(info)

    try:
        sent = None
        if poster:
            try:
                sent = await StreamBot.send_photo(chat, poster, caption=caption,
                                           parse_mode=ParseMode.HTML, reply_markup=markup)
            except FloodWait:
                raise
            except Exception:
                sent = None
        if sent is None:
            sent = await StreamBot.send_message(chat, caption, parse_mode=ParseMode.HTML,
                                     reply_markup=markup, disable_web_page_preview=True)
        if sent is not None:
            await _store_announcement_msg(info.get("media_type"), info.get("tmdb_id"), chat, sent.id)
    except FloodWait as e:
        LOGGER.warning(f"Announcement FloodWait for {e.value}s")
    except Exception as e:
        LOGGER.error(f"Announcement failed for '{info.get('title')}': {e}")


#----- Fire-and-forget announcement for a freshly added title
def announce_new_media(info: dict) -> None:
    try:
        create_task(_announce(dict(info)))
    except RuntimeError:
        LOGGER.warning("Announcement skipped: no running event loop.")


#----- Delete the announcement message when media is removed from the library
async def delete_announcement(media_type: str, tmdb_id) -> None:
    if not tmdb_id:
        return
    key = f"{media_type}:{tmdb_id}"
    try:
        doc = await db.dbs["tracking"]["announced"].find_one_and_delete({"_id": key})
    except Exception as e:
        LOGGER.warning(f"Failed to lookup announcement for {key}: {e}")
        return
    if not doc:
        return
    chat_id = doc.get("chat_id")
    message_id = doc.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        await StreamBot.delete_messages(chat_id, message_id)
        LOGGER.info(f"Deleted announcement message {message_id} for {key}")
    except (MessageDeleteForbidden, MessageIdInvalid) as e:
        LOGGER.warning(f"Could not delete announcement {message_id} for {key}: {e}")
    except FloodWait as e:
        LOGGER.warning(f"FloodWait deleting announcement for {key}: {e.value}s")
    except Exception as e:
        LOGGER.warning(f"Failed to delete announcement message for {key}: {e}")


def delete_announcement_async(media_type: str, tmdb_id) -> None:
    try:
        create_task(delete_announcement(media_type, tmdb_id))
    except RuntimeError:
        LOGGER.warning("Announcement delete skipped: no running event loop.")
