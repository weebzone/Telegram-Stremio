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
from Backend.helper.pyro import get_readable_file_size
from Backend.pyrofork.bot import Userbot

MAX_RESULTS = 50
MAX_RESULTS_PER_CHAT = 50
SEARCH_COOLDOWN_SECONDS = 5
MAX_CONCURRENT_SEARCHES = 3
MIN_TITLE_SCORE = 0.6

_last_search_ts: Dict[str, float] = {}
_inflight_queries: set = set()
_search_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
_userbot_session_dead = False
_chat_title_cache: Dict[int, str] = {}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MULTIPART_RE = re.compile(r"(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)", re.IGNORECASE)
_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv", ".webm", ".flv")


def is_userbot_available() -> bool:
    return Userbot is not None and not _userbot_session_dead


def is_global_search_enabled() -> bool:
    if not is_userbot_available():
        return False
    s = SettingsManager.current()
    return bool(s.global_search and s.global_search_channels)


def _tokens(s: str) -> set:
    return set(_TOKEN_RE.findall((s or "").lower()))


def _title_score(result_title: str, expected_title: str) -> float:
    expected = _tokens(expected_title)
    return len(expected & _tokens(result_title)) / len(expected) if expected else 0.0


def _matches_episode(parsed: dict, season: Optional[int], episode: Optional[int]) -> bool:
    for value, parsed_key in ((season, "season"), (episode, "episode")):
        if value is None:
            continue
        rv = parsed.get(parsed_key)
        if rv is None:
            continue
        if isinstance(rv, list):
            if value not in rv:
                return False
        elif int(rv) != int(value):
            return False
    return True


def _parse_and_validate(filename: str, expected_title: str, season: Optional[int], episode: Optional[int]) -> Optional[dict]:
    if _MULTIPART_RE.search(filename):
        LOGGER.info(f"Skipping {filename}: seems to be a split video file")
        return None

    try:
        parsed = PTN.parse(filename)
    except Exception:
        return None

    if "excess" in parsed and any("combined" in item.lower() for item in parsed["excess"]):
        LOGGER.info(f"Skipping {filename}: contains 'combined'")
        return None

    if not _matches_episode(parsed, season, episode):
        return None
    if _title_score(parsed.get("title", ""), expected_title) < MIN_TITLE_SCORE:
        return None
    return parsed


def _video_filename(message) -> Optional[str]:
    if message.video:
        return (message.caption or "").strip() or getattr(message.video, "file_name", None) or "video.mkv"
    if message.document:
        mime = message.document.mime_type or ""
        name = message.document.file_name
        if mime.startswith("video/") or (name and name.lower().endswith(_VIDEO_EXTS)):
            return (message.caption or "").strip() or name or "video.mkv"
    return None


def _resolve_channel_ids(channel_ids: List[str]) -> List[int]:
    resolved: List[int] = []
    seen: set = set()
    for c in channel_ids:
        c = str(c).strip()
        if not c:
            continue
        try:
            raw = int(c)
        except ValueError:
            continue
        canonical = raw if raw < 0 else int(f"-100{raw}")
        if canonical not in seen:
            seen.add(canonical)
            resolved.append(canonical)
    return resolved


async def _get_chat_title(client, chat_id: int) -> str:
    if chat_id in _chat_title_cache:
        return _chat_title_cache[chat_id]
    try:
        chat = await client.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)
    _chat_title_cache[chat_id] = title
    return title


async def _search_channel(
    client,
    chat_id: int,
    chat_title: str,
    search_query: str,
    expected_title: str,
    season: Optional[int],
    episode: Optional[int],
) -> List[Dict]:
    global _userbot_session_dead
    results: List[Dict] = []
    seen_msg_ids: set = set()

    for msg_filter in (enums.MessagesFilter.VIDEO, enums.MessagesFilter.DOCUMENT):
        if len(results) >= MAX_RESULTS_PER_CHAT:
            break
        try:
            async for message in client.search_messages(
                chat_id=chat_id,
                query=search_query,
                filter=msg_filter,
                limit=MAX_RESULTS_PER_CHAT,
            ):
                if message.id in seen_msg_ids:
                    continue
                seen_msg_ids.add(message.id)

                filename = _video_filename(message)
                if not filename:
                    continue

                parsed = _parse_and_validate(filename, expected_title, season, episode)
                if parsed is None:
                    continue

                media = message.video or message.document
                size = get_readable_file_size(getattr(media, "file_size", 0) or 0)
                quality = parsed.get("resolution") or "HD"

                token = await encode_string({
                    "global": True,
                    "chat_id": chat_id,
                    "msg_id": message.id,
                    "title": filename,
                    "size": size,
                    "quality": quality,
                    "source": chat_title,
                })

                results.append({
                    "token": token,
                    "title": filename,
                    "size": size,
                    "source_chat": chat_title,
                    "quality": quality,
                })
                LOGGER.debug(f"[GLOBAL SEARCH] Result found: {filename} in {chat_title}")

                if len(results) >= MAX_RESULTS_PER_CHAT:
                    break

        except FloodWait as e:
            LOGGER.warning(f"[USERBOT] FloodWait for {chat_title}: sleeping {e.value}s")
            await asyncio.sleep(e.value)
        except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UserNotParticipant) as e:
            LOGGER.warning(f"[USERBOT] Cannot access channel {chat_title}: {type(e).__name__}")
            break
        except (AuthKeyUnregistered, SessionRevoked) as e:
            LOGGER.error(f"[USERBOT] Session invalid ({type(e).__name__}): {e}")
            _userbot_session_dead = True
            break
        except RPCError as e:
            LOGGER.warning(f"[USERBOT] RPC error in {chat_title} ({msg_filter}): {e}")

    return results


async def global_search(
    expected_title: str,
    auth_channels: List[str],
    *,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> List[Dict]:
    expected_title = (expected_title or "").strip()
    if not expected_title or not is_global_search_enabled():
        return []

    settings = SettingsManager.current()
    target_ids = _resolve_channel_ids(settings.global_search_channels)
    if not target_ids:
        return []

    if season is not None and episode is not None:
        search_query = f"{expected_title} S{int(season):02d}E{int(episode):02d}"
    elif year is not None:
        search_query = f"{expected_title} {year}"
    else:
        search_query = expected_title

    key = search_query.lower()
    now = time.time()
    if now - _last_search_ts.get(key, 0) < SEARCH_COOLDOWN_SECONDS:
        LOGGER.info(f"[GLOBAL SEARCH] Cooldown active for '{search_query}'")
        return []
    if key in _inflight_queries:
        LOGGER.info(f"[GLOBAL SEARCH] Duplicate in-flight for '{search_query}'")
        return []

    _inflight_queries.add(key)
    _last_search_ts[key] = now

    try:
        async with _search_semaphore:
            LOGGER.info(f"[USERBOT] Search started: '{search_query}' across {len(target_ids)} channel(s)")
            chat_titles = await asyncio.gather(
                *(_get_chat_title(Userbot, cid) for cid in target_ids),
                return_exceptions=True,
            )

            search_tasks = []
            for cid, title in zip(target_ids, chat_titles):
                if _userbot_session_dead:
                    break
                if isinstance(title, Exception):
                    title = str(cid)
                search_tasks.append(
                    _search_channel(Userbot, cid, title, search_query, expected_title, season, episode)
                )

            per_channel_results = await asyncio.gather(*search_tasks, return_exceptions=True)

            all_results: List[Dict] = []
            for r in per_channel_results:
                if isinstance(r, list):
                    all_results.extend(r)
            all_results = all_results[:MAX_RESULTS]

            LOGGER.info(f"[USERBOT] Search completed: '{search_query}' -> {len(all_results)} result(s)")
            return all_results
    finally:
        _inflight_queries.discard(key)
