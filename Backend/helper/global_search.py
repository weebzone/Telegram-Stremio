import asyncio
import re
import time
from typing import Dict, List, Optional

import PTN
from pyrogram import enums
from pyrogram.errors import (
    FloodWait,
    ChatAdminRequired,
    ChannelPrivate,
    PeerIdInvalid,
    UserNotParticipant,
    AuthKeyUnregistered,
    SessionRevoked,
    RPCError,
)

from Backend.logger import LOGGER
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.encrypt import encode_string

MAX_RESULTS = 50
MAX_RESULTS_PER_CHAT = 50
MAX_DIALOGS_SCANNED = 200
SEARCH_COOLDOWN_SECONDS = 5
MAX_CONCURRENT_SEARCHES = 3
MIN_TITLE_SCORE = 0.6              

_last_search_ts: Dict[str, float] = {}
_inflight_queries: set = set()
_search_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
_userbot_session_dead = False

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def is_userbot_available() -> bool:
    from Backend.pyrofork.bot import Userbot
    return Userbot is not None and not _userbot_session_dead


def is_global_search_enabled() -> bool:
    return is_userbot_available() and SettingsManager.current().global_search


def _readable_size(num_bytes: int) -> str:
    from Backend.helper.pyro import get_readable_file_size
    return get_readable_file_size(num_bytes)


def _video_filename(message) -> Optional[str]:
    """Returns a usable filename if this message is video content, else
    None."""
    if message.video:
        return message.caption or getattr(message.video, "file_name", None) or "video.mkv"
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("video/"):
            return message.caption or message.document.file_name or "video.mkv"
    return None


def _tokens(s: str) -> set:
    return set(_TOKEN_RE.findall((s or "").lower()))


def _title_score(result_title: str, expected_title: str) -> float:
    expected = _tokens(expected_title)
    if not expected:
        return 0.0
    found = _tokens(result_title)
    return len(expected & found) / len(expected)


def _matches_episode(parsed: dict, season: Optional[int], episode: Optional[int]) -> bool:
    """Validates a PTN-parsed filename against the requested season/episode
    (TV only). A filename that doesn't mention season/episode at all is
    treated as ambiguous, not a mismatch — single-episode uploads in some
    channels omit it from the filename. An explicit, conflicting value is a
    hard reject."""
    if season is not None:
        result_season = parsed.get("season")
        if isinstance(result_season, list):
            if season not in result_season:
                return False
        elif result_season is not None and int(result_season) != int(season):
            return False

    if episode is not None:
        result_episode = parsed.get("episode")
        if isinstance(result_episode, list):
            if episode not in result_episode:
                return False
        elif result_episode is not None and int(result_episode) != int(episode):
            return False

    return True


def _parse_and_validate(filename: str, expected_title: str, season: Optional[int], episode: Optional[int]) -> Optional[dict]:
    """Properly parses `filename` with PTN and returns the parsed dict only
    if it plausibly matches what we're looking for, else None."""
    try:
        parsed = PTN.parse(filename)
    except Exception:
        return None

    if not _matches_episode(parsed, season, episode):
        return None

    result_title = parsed.get("title", "")
    if _title_score(result_title, expected_title) < MIN_TITLE_SCORE:
        return None

    return parsed


def _auth_channel_ids(auth_channels: List[str]) -> set:
    ids = set()
    for c in auth_channels:
        c = str(c).strip()
        if not c:
            continue
        try:
            ids.add(int(c))
        except ValueError:
            continue
        try:
            ids.add(int(f"-100{c}"))
        except ValueError:
            continue
    return ids


async def global_search(
    expected_title: str,
    auth_channels: List[str],
    *,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> List[Dict]:
    """Search for media matching `expected_title` (optionally a specific
    `season`/`episode` for TV) across every chat the Userbot can see,
    excluding Auth Channels. Returns up to MAX_RESULTS dicts:
        {token, title, size, source_chat, quality}
    `token` is an opaque playback id — pass it straight into the same
    encode/decode pipeline used for normal streams (see stream_routes.py).
    Every candidate is parsed with PTN and validated against
    expected_title/season/episode before being included — Telegram's own
    search is loose and returns plenty of unrelated matches otherwise."""
    expected_title = (expected_title or "").strip()
    if not expected_title:
        return []

    if not is_global_search_enabled():
        return []

    from Backend.pyrofork.bot import Userbot

    search_query = expected_title
    if season is not None and episode is not None:
        search_query = f"{expected_title} S{int(season):02d}E{int(episode):02d}"

    key = f"{search_query.lower()}"
    now = time.time()
    if now - _last_search_ts.get(key, 0) < SEARCH_COOLDOWN_SECONDS:
        LOGGER.info(f"[GLOBAL SEARCH] Cooldown active for '{search_query}', skipping.")
        return []
    if key in _inflight_queries:
        LOGGER.info(f"[GLOBAL SEARCH] Duplicate in-flight search for '{search_query}', skipping.")
        return []

    _inflight_queries.add(key)
    _last_search_ts[key] = now

    excluded_ids = _auth_channel_ids(auth_channels)
    results: List[Dict] = []

    try:
        async with _search_semaphore:
            LOGGER.info(f"[USERBOT] Search started: '{search_query}'")
            dialogs_scanned = 0

            try:
                async for dialog in Userbot.get_dialogs(limit=MAX_DIALOGS_SCANNED):
                    if len(results) >= MAX_RESULTS:
                        break

                    chat = dialog.chat
                    if not chat or chat.id in excluded_ids:
                        continue
                    if chat.type not in (
                        enums.ChatType.CHANNEL,
                        enums.ChatType.SUPERGROUP,
                        enums.ChatType.GROUP,
                    ):
                        continue

                    dialogs_scanned += 1
                    found_in_chat = 0

                    try:
                        async for message in Userbot.search_messages(
                            chat_id=chat.id,
                            query=search_query,
                            filter=enums.MessagesFilter.VIDEO,
                            limit=MAX_RESULTS_PER_CHAT,
                        ):
                            filename = _video_filename(message)
                            if not filename:
                                continue

                            parsed = _parse_and_validate(filename, expected_title, season, episode)
                            if parsed is None:
                                continue

                            media = message.video or message.document
                            size_bytes = getattr(media, "file_size", 0) or 0
                            quality = parsed.get("resolution") or "HD"

                            token = await encode_string({
                                "global": True,
                                "chat_id": chat.id,
                                "msg_id": message.id,
                                "title": filename,
                                "size": _readable_size(size_bytes),
                                "quality": quality,
                                "source": chat.title or "Unknown",
                            })

                            results.append({
                                "token": token,
                                "title": filename,
                                "size": _readable_size(size_bytes),
                                "source_chat": chat.title or "Unknown",
                                "quality": quality,
                            })

                            LOGGER.info(f"[GLOBAL SEARCH] Result found: {filename} in {chat.title}")

                            found_in_chat += 1
                            if found_in_chat >= MAX_RESULTS_PER_CHAT or len(results) >= MAX_RESULTS:
                                break

                    except FloodWait as e:
                        LOGGER.warning(f"[USERBOT] FloodWait detected: sleeping {e.value}s")
                        await asyncio.sleep(e.value)
                        continue
                    except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UserNotParticipant) as e:
                        LOGGER.debug(f"[USERBOT] Skipping inaccessible chat {chat.id}: {type(e).__name__}")
                        continue
                    except (AuthKeyUnregistered, SessionRevoked) as e:
                        global _userbot_session_dead
                        _userbot_session_dead = True
                        LOGGER.error(f"[USERBOT] Session invalid ({type(e).__name__}): {e}. Global Search disabled for this run.")
                        break
                    except RPCError as e:
                        LOGGER.warning(f"[USERBOT] RPC error searching chat {chat.id}: {e}")
                        continue

            except FloodWait as e:
                LOGGER.warning(f"[USERBOT] FloodWait detected while listing dialogs: sleeping {e.value}s")
                await asyncio.sleep(e.value)

            LOGGER.info(
                f"[USERBOT] Search completed: '{search_query}' -> {len(results)} result(s) "
                f"across {dialogs_scanned} chat(s)"
            )
            return results
    finally:
        _inflight_queries.discard(key)
