from asyncio import create_task
from datetime import datetime

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageDeleteForbidden, MessageIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Backend import db
from Backend.helper.metadata.providers.tmdb import get_tmdb_client
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


def _resolve_thread(value) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


#----- Atomically claim a key so it is announced at most once; returns True if newly claimed
async def _claim(key: str) -> bool:
    if not key:
        return False
    result = await db.dbs["tracking"]["announced"].update_one(
        {"_id": key},
        {"$setOnInsert": {"at": datetime.utcnow()}},
        upsert=True,
    )
    return result.upserted_id is not None


async def _store_announcement_msg(key: str, chat_id, message_id: int) -> None:
    if not key or not message_id:
        return
    try:
        await db.dbs["tracking"]["announced"].update_one(
            {"_id": key},
            {"$set": {"chat_id": chat_id, "message_id": message_id, "at": datetime.utcnow()}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"Failed to store announcement message id: {e}")


#----- TMDB helpers (TMDB is the source of truth; we never infer "airing" from counts) -----
_TMDB_STATUS_CACHE = {}
_TMDB_SEASON_TOTAL_CACHE = {}


async def _tmdb_series_status(tmdb_id) -> str:
    """Return the TV series status string from TMDB (e.g. 'Ended', 'Returning Series')."""
    if not tmdb_id:
        return ""
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return ""
    if tmdb_id in _TMDB_STATUS_CACHE:
        return _TMDB_STATUS_CACHE[tmdb_id]
    status = ""
    try:
        client = get_tmdb_client()
        show = await client.tv(tmdb_id).details()
        status = (getattr(show, "status", None) or "").strip()
    except Exception as e:
        LOGGER.warning(f"TMDB series status failed for {tmdb_id}: {e}")
    _TMDB_STATUS_CACHE[tmdb_id] = status
    return status


async def _tmdb_season_total(tmdb_id, season_number) -> int:
    """Total number of episodes TMDB lists for this season (source of truth for 'complete')."""
    if not tmdb_id or not season_number:
        return 0
    try:
        tmdb_id = int(tmdb_id)
        season_number = int(season_number)
    except (TypeError, ValueError):
        return 0
    cache_key = (tmdb_id, season_number)
    if cache_key in _TMDB_SEASON_TOTAL_CACHE:
        return _TMDB_SEASON_TOTAL_CACHE[cache_key]
    total = 0
    try:
        client = get_tmdb_client()
        season = await client.tv(tmdb_id).season(season_number).details()
        episodes = getattr(season, "episodes", None) or []
        total = len(episodes)
    except Exception as e:
        LOGGER.warning(f"TMDB season total failed for {tmdb_id} S{season_number}: {e}")
    _TMDB_SEASON_TOTAL_CACHE[cache_key] = total
    return total


async def _count_season_episodes(tmdb_id, season_number) -> int:
    """How many episodes of this season already have a telegram source in the DB."""
    if not tmdb_id or not season_number:
        return 0
    try:
        tmdb_id = int(tmdb_id)
        season_number = int(season_number)
    except (TypeError, ValueError):
        return 0
    total = 0
    try:
        for i in range(1, db.current_db_index + 1):
            coll = db.dbs[f"storage_{i}"]["tv"]
            doc = await coll.find_one({"tmdb_id": tmdb_id}, {"seasons": 1})
            if not doc:
                continue
            for s in doc.get("seasons", []):
                if int(s.get("season_number", 0)) != season_number:
                    continue
                for ep in s.get("episodes", []):
                    if ep.get("telegram"):
                        total += 1
    except Exception as e:
        LOGGER.warning(f"_count_season_episodes failed: {e}")
    return total


def _build_caption(info: dict, season_complete: bool = False, ep_count: int = 0) -> str:
    is_tv = info.get("media_type") == "tv"
    title = info.get("title") or "Unknown"

    if season_complete:
        season_number = int(info.get("season_number", 0) or 0)
        header = f"📺 <b>Temporada completa</b>\n\n<b>{title}</b>"
        lines = [header, ""]
        if season_number:
            lines.append(f"📦 <b>{ep_count} episodios agregados</b> de la temporada {season_number}")
        else:
            lines.append(f"📦 <b>{ep_count} episodios agregados</b>")
        if info.get("quality"):
            lines.append(f"📶 <b>Calidad:</b> {info['quality']}")
        return "\n".join(lines)

    # Single-episode / movie announcement (style of the reference screenshot)
    icon = "📺" if is_tv else "🎬"
    label = "Nuevo episodio" if is_tv else "Nueva película"
    header = f"{icon} <b>{label}</b>\n\n<b>{title}</b>"
    if info.get("year"):
        header += f" ({info['year']})"

    lines = [header, ""]
    if is_tv and info.get("season_number") and info.get("episode_number"):
        se = f"S{int(info['season_number']):02d}E{int(info['episode_number']):02d}"
        ep_title = info.get("episode_title")
        lines.append(f"📺 <b>{se}</b>" + (f" — {ep_title}" if ep_title else ""))
    if info.get("rate"):
        try:
            lines.append(f"⭐ <b>Rating:</b> {round(float(info['rate']), 1)}")
        except (TypeError, ValueError):
            pass
    genres = info.get("genres") or []
    if genres:
        lines.append(f"🎭 <b>Géneros:</b> {', '.join(genres[:4])}")
    if info.get("quality"):
        lines.append(f"📶 <b>Calidad:</b> {info['quality']}")

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
    thread = _resolve_thread(settings.announcement_thread)

    media_type = info.get("media_type")
    tmdb_id = info.get("tmdb_id")
    season_number = info.get("season_number")

    season_complete = False
    ep_count = 0
    announce_key = None

    # Announce every freshly added item immediately.
    # TV: key by season (and episode when present) so each added season/episode
    # notifies, regardless of whether episode_number was parsed.
    if media_type == "tv":
        ep = info.get("episode_number")
        if season_number and ep:
            announce_key = f"tv:{tmdb_id}:s{season_number}e{ep}"
        elif season_number:
            announce_key = f"tv:{tmdb_id}:s{season_number}"
        else:
            announce_key = f"tv:{tmdb_id}"
    else:
        announce_key = f"{media_type}:{tmdb_id}"

    if not await _claim(announce_key):
        return

    caption = _build_caption(info, season_complete, ep_count)
    poster = info.get("backdrop") or info.get("poster")
    markup = _build_markup(info)

    try:
        sent = None
        if poster:
            try:
                sent = await StreamBot.send_photo(chat, poster, caption=caption,
                                       parse_mode=ParseMode.HTML, reply_markup=markup,
                                       message_thread_id=thread)
            except FloodWait:
                raise
            except Exception:
                sent = None
        if sent is None:
            sent = await StreamBot.send_message(chat, caption, parse_mode=ParseMode.HTML,
                                 reply_markup=markup, disable_web_page_preview=True,
                                 message_thread_id=thread)
        if sent is not None:
            await _store_announcement_msg(announce_key, chat, sent.id)
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
