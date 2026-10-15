"""Notify a Telegram group/channel when a new title is requested via the
public Request page. Mirrors the pattern used in announcer.py, but fires on
*new* requests (submit_request reason == "created") instead of new uploads.
"""
import time as _time
from asyncio import create_task

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot


#----- Deduplicate Telegram notifications: Stremio retries the stream GET, so
# the same request can enter here 7x in 5s. We only want ONE Telegram message
# per (imdb, season, episode) per ~5 second window. SAME TTL as external_api_notifier.
from Backend.helper.external_api_notifier import _notify_key, _is_recently_sent, _RECENT_NOTIFY, _NOTIFY_TTL  # reuse


def _resolve_chat(value: str):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _resolve_thread(value) -> int | None:
    value = str(getattr(value, "request_notify_thread", value) or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _build_caption(doc: dict) -> str:
    is_tv = doc.get("media_type") == "tv"
    title = doc.get("title") or "Unknown"
    header = f"🆕 <b>Nuevo pedido</b>\n\n{'📺' if is_tv else '🎬'} <b>{title}</b>"
    if doc.get("year"):
        header += f" ({doc['year']})"

    lines = [header, "", f"🗂 <b>Tipo:</b> {'Serie' if is_tv else 'Película'}"]
    #----- Requested seasons (TV): "Temporadas: T1, T3" or "Serie completa" when unspecified
    if is_tv:
        seasons = [s for s in (doc.get("season_numbers") or []) if s]
        if seasons:
            lines.append("📺 <b>Temporadas:</b> " + ", ".join(f"T{s}" for s in sorted(seasons)))
        else:
            lines.append("📺 <b>Temporadas:</b> Serie completa")
    imdb_id = doc.get("imdb_id")
    if imdb_id:
        lines.append(f"🆔 <b>IMDb:</b> {imdb_id}")
    lines.append("👤 Pedido por un usuario en la página de Requests.")
    return "\n".join(lines)


def _build_markup(doc: dict):
    rows = []
    tmdb_id = doc.get("tmdb_id")
    if tmdb_id:
        media_path = "tv" if doc.get("media_type") == "tv" else "movie"
        rows.append([InlineKeyboardButton(
            "🔎 Ver en TMDB",
            url=f"https://www.themoviedb.org/{media_path}/{tmdb_id}",
        )])
    imdb_id = doc.get("imdb_id")
    if imdb_id:
        rows.append([InlineKeyboardButton(
            "🅰️ Ver en IMDb",
            url=f"https://www.imdb.com/title/{imdb_id}",
        )])
    base = SettingsManager.current().base_url
    if base:
        rows.append([InlineKeyboardButton("⚙️ Panel de Requests", url=f"{base}/requests")])
    return InlineKeyboardMarkup(rows) if rows else None


async def _notify(doc: dict) -> None:
    settings = SettingsManager.current()
    chat = _resolve_chat(settings.request_notify_channel)
    if not settings.notify_new_requests or chat is None:
        return
    thread = _resolve_thread(settings.request_notify_thread)

    caption = _build_caption(doc)
    poster = doc.get("poster")
    markup = _build_markup(doc)

    try:
        sent = None
        if poster:
            try:
                sent = await StreamBot.send_photo(
                    chat, poster, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=markup,
                    message_thread_id=thread,
                )
            except FloodWait:
                raise
            except Exception:
                sent = None
        if sent is None:
            await StreamBot.send_message(
                chat, caption, parse_mode=ParseMode.HTML,
                reply_markup=markup, disable_web_page_preview=True,
                message_thread_id=thread,
            )
    except FloodWait as e:
        LOGGER.warning(f"Request notification FloodWait for {e.value}s")
    except Exception as e:
        LOGGER.error(f"Request notification failed for '{doc.get('title')}': {e}")


#----- Fire-and-forget notification for a freshly created request (call once per new title)
def notify_new_request(doc: dict) -> None:
    # Dedupe: Stremio retries the stream GET → 7x notify. Only send ONCE per
    # (imdb, season, episode) per _NOTIFY_TTL (~5s). Reuses the same cache as
    # external_api_notifier so webhook + Telegram dedupe stay in sync.
    key = _notify_key(doc, namespace="telegram")  # namespace isolates from webhook dedupe
    if _is_recently_sent(key):
        LOGGER.info(
            f"Request notify SKIPPED (duplicate): '{doc.get('title')}' key={key}"
        )
        return
    try:
        create_task(_notify(dict(doc)))
    except RuntimeError:
        LOGGER.warning("notify_new_request called outside an event loop; skipped")


#----- Reusable hook fired by the "Solicitar contenido" stream prompt clicked
# from the Stremio player. Resolves the imdb_id via Cinemeta (same resolver as
# the public /requests page) and delegates to submit_request, so the n8n /requests
# webhook receives the EXACT same payload. Returns the same dict submit_request
# returns: {"ok": True, "reason": ...}.
async def queue_stream_request(media_id: str, token_data: dict | None, referer: str) -> dict:
    from Backend.helper import requests_manager as _rm
    from Backend.fastapi.routes.stremio_routes import _parse_stremio_id
    # Parse Stremio media_id:  película = "tt0468569", serie = "tt0944947:1:5" (imdb:season:episode)
    # Reuses the same parser get_streams() uses so season/episode extraction is identical.
    try:
        parsed = _parse_stremio_id(media_id)
    except Exception:
        parsed = {"imdb_id": media_id, "season_num": None, "episode_num": None}
    imdb_id = parsed["imdb_id"] or media_id
    season_num = parsed.get("season_num")
    episode_num = parsed.get("episode_num")
    # Resolve title/type/tmdb_id/poster/year via Cinemeta (movie + tv attempts)
    hits = await _rm._cinemeta_id_search(imdb_id) if imdb_id else []
    if not hits:
        # Fallback: try a name search on the imdb id itself
        hits = await _rm._cinemeta_name_search(imdb_id)
    hit = hits[0] if hits else None
    # Force media_type=tv when a season/episode was parsed — Cinemeta may return
    # a false-positive "movie" hit first (e.g. tt0944947 Rick & Morty). The presence
    # of season_num/episode_num definitively means it's a TV episode.
    if season_num:
        hit = next((h for h in hits if h.get("media_type") == "tv"), hit or {})
        forced_type = "tv"
    else:
        forced_type = None
    # Only send season_numbers when a specific season was requested (Serie/Temporada/Episodio)
    seasons = [season_num] if season_num else []
    result = await _rm.submit_request(
        media_type=(forced_type or (hit["media_type"] if hit else "movie")),
        tmdb_id=(hit["tmdb_id"] if hit else None),
        imdb_id=imdb_id,
        title=(hit["title"] if hit else imdb_id),
        year=(hit["year"] if hit else None),
        poster=(hit["poster"] if hit else ""),
        client_ip=None,           # unknown from Stremio player; hash stays empty
        season_numbers=seasons,
        episode_num=episode_num,  # → webhook recibirá el número de episodio
    )
    return result or {"ok": False, "reason": "unresolved"}

