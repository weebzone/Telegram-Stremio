from asyncio import create_task
from datetime import datetime

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Backend import db
from Backend.helper.settings_manager import SettingsManager
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


#----- Atomically claim a title so it is announced at most once
async def _claim(media_type: str, tmdb_id) -> bool:
    if not tmdb_id:
        return False
    result = await db.dbs["tracking"]["announced"].update_one(
        {"_id": f"{media_type}:{tmdb_id}"},
        {"$setOnInsert": {"at": datetime.utcnow()}},
        upsert=True,
    )
    return result.upserted_id is not None


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


async def _announce(info: dict) -> None:
    settings = SettingsManager.current()
    chat = _resolve_chat(settings.announcement_channel)
    if not settings.announce_new_content or chat is None:
        return
    if not await _claim(info.get("media_type"), info.get("tmdb_id")):
        return

    caption = _build_caption(info)
    poster = info.get("backdrop") or info.get("poster")
    markup = None
    bot_url = get_streambot_url()
    if bot_url and bot_url != "https://t.me/":
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Watch on Stremio", url=bot_url)]])

    try:
        if poster:
            try:
                await StreamBot.send_photo(chat, poster, caption=caption,
                                           parse_mode=ParseMode.HTML, reply_markup=markup)
                return
            except FloodWait:
                raise
            except Exception:
                pass
        await StreamBot.send_message(chat, caption, parse_mode=ParseMode.HTML,
                                     reply_markup=markup, disable_web_page_preview=True)
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
