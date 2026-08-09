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
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix
import Backend.pyrofork.bot as botmod

MAX_RESULTS = 50
MAX_RESULTS_PER_CHAT = 50
SEARCH_COOLDOWN_SECONDS = 5
MAX_CONCURRENT_SEARCHES = 3
MAX_CONCURRENT_CHANNELS = 5
MIN_TITLE_SCORE = 0.7
RESULT_CACHE_SECONDS = 60

_last_search_ts: Dict[str, float] = {}
_inflight_tasks: Dict[str, asyncio.Task] = {}
_result_cache: Dict[str, tuple] = {} 
_search_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
_channel_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHANNELS)
_userbot_session_dead = False
_chat_title_cache: Dict[int, str] = {}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MULTIPART_RE = re.compile(r"(?:part|cd|disc|disk)[s._-]*\d+(?=\.\w+$)", re.IGNORECASE)
_ALT_PART_RE = re.compile(r"^(.*?)[\s._-]*(?:part|cd|disc|disk|pt)[\s._-]*0*(\d{1,3})(?=\.\w+$|$)", re.IGNORECASE)
_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv", ".webm", ".flv")
SPLIT_SCAN_WINDOW = 20
_APOSTROPHE_RE = re.compile(r"['\u2018\u2019`\u00B4]")
_SYMBOL_STRIP_RE = re.compile(r"[&.\-:]+")


def is_userbot_available() -> bool:
    return botmod.Userbot is not None and not _userbot_session_dead


def is_global_search_enabled() -> bool:
    if not is_userbot_available():
        return False
    s = SettingsManager.current()
    return bool(s.global_search)


def _tokens(s: str) -> set:
    return set(_TOKEN_RE.findall((s or "").lower()))


def _title_score(
    result_title: str,
    expected_title: str,
) -> float:
    expected = _tokens(expected_title)
    result = _tokens(result_title)

    if not expected or not result:
        return 0.0

    common = len(expected & result)

    if common == 0:
        return 0.0

    precision = common / len(result)
    recall = common / len(expected)

    return (2 * precision * recall) / (precision + recall)

_ABS_EP_BOUNDARY_RE = re.compile(
    r"(?i)(?:^|[^0-9])(?:e|ep|episode)?\s*0*(\d{1,4})(?=[^0-9]|$)"
)


def _filename_has_exact_episode(filename: str, episode: int) -> bool:
    if not filename or episode is None:
        return False
    target = int(episode)
    for m in _ABS_EP_BOUNDARY_RE.finditer(filename):
        try:
            if int(m.group(1)) == target:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _matches_episode(parsed: dict, season: Optional[int], episode: Optional[int], filename: str = "") -> bool:
    wants_episode = season is not None or episode is not None
    is_episode_like = parsed.get("season") is not None or parsed.get("episode") is not None

    if season is None and episode is not None:
        rv = parsed.get("episode")
        if rv is not None:
            if isinstance(rv, list):
                try:
                    if int(episode) in [int(x) for x in rv]:
                        return True
                except (TypeError, ValueError):
                    pass
            else:
                try:
                    if int(rv) == int(episode):
                        return True
                except (TypeError, ValueError):
                    pass
        return _filename_has_exact_episode(filename, int(episode))

    if wants_episode and not is_episode_like:
        return False
    if not wants_episode and is_episode_like:
        return False

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


def _is_combined_filename(filename: str, parsed: Optional[dict] = None) -> bool:
    if not filename:
        return False
    if parse_combined_episodes(filename):
        return True
    if re.search(r"(?i)\bcombined\b", filename):
        return True
    if parsed and "excess" in parsed:
        if any("combined" in str(item).lower() for item in (parsed.get("excess") or [])):
            return True
    return False


def _validate_name(filename: str, expected_title: str, season: Optional[int], episode: Optional[int]) -> Optional[dict]:
    try:
        parsed = PTN.parse(filename)
    except Exception:
        return None
    if _is_combined_filename(filename, parsed):
        LOGGER.info(f"Skipping {filename}: combined episode pack")
        return None
    if not _matches_episode(parsed, season, episode, filename=filename):
        return None

    result_title = parsed.get("title", "")

    score = _title_score(result_title, expected_title)
    if score < MIN_TITLE_SCORE:
        stripped_expected = _strip_symbols(expected_title)
        if stripped_expected and stripped_expected.lower() != expected_title.lower():
            score = _title_score(result_title, stripped_expected)

    if score < MIN_TITLE_SCORE:
        return None
    return parsed


def _parse_and_validate(filename: str, expected_title: str, season: Optional[int], episode: Optional[int]) -> Optional[dict]:
    if _MULTIPART_RE.search(filename):
        return None
    return _validate_name(filename, expected_title, season, episode)


def _split_part_info(filename: str) -> Optional[tuple]:
    if not filename:
        return None
    zm = _ZIP_SPLIT_RE.match(filename)
    if zm:
        base_raw = zm.group("base")
        base = _NORMALIZE_ALT_RE.sub(".", base_raw).strip(".").lower() + ".zip"
        return base, int(zm.group("num")), base_raw, True
    info = parse_split_info(filename)
    if info:
        return info[0], info[1], strip_part_suffix(filename), False
    m = _ALT_PART_RE.match(filename)
    if m and m.group(1).strip(" ._-"):
        ext_m = re.search(r"(\.\w+)$", filename)
        display = m.group(1).strip(" ._-") + (ext_m.group(1) if ext_m else ".mkv")
        base = _NORMALIZE_ALT_RE.sub(".", m.group(1)).strip(".").lower()
        return base, int(m.group(2)), display, False
    return None


_NORMALIZE_ALT_RE = re.compile(r"[\s._-]+")
_ZIP_SPLIT_RE = re.compile(r"^(?P<base>.+)\.zip\.(?P<num>\d{2,3})$", re.IGNORECASE)


def _video_filename(message) -> Optional[str]:
    if message.video:
        return (message.caption or "").strip() or getattr(message.video, "file_name", None) or "video.mkv"
    if message.document:
        mime = message.document.mime_type or ""
        name = message.document.file_name
        # Include single .zip archives (STORED inner video is streamable)
        if mime.startswith("video/") or (name and (name.lower().endswith(_VIDEO_EXTS) or name.lower().endswith(".zip"))):
            return (message.caption or "").strip() or name or "video.mkv"
    return None


def _raw_media_name(message) -> Optional[str]:
    media = message.video or message.document
    if not media:
        return None
    return (message.caption or "").strip() or getattr(media, "file_name", None)


async def _gather_split_parts(client, chat_id: int, seed_id: int, base: str) -> Dict[int, dict]:
    ids = list(range(max(1, seed_id - SPLIT_SCAN_WINDOW), seed_id + SPLIT_SCAN_WINDOW + 1))
    parts: Dict[int, dict] = {}
    t0 = time.monotonic()
    try:
        messages = await client.get_messages(chat_id, ids)
    except Exception as e:
        LOGGER.warning(f"[GLOBAL SEARCH] Could not gather split parts near {seed_id}: {e}")
        return parts
    finally:
        elapsed = time.monotonic() - t0
        LOGGER.info(f"[GLOBAL SEARCH] get_messages({len(ids)} ids, base={base}) took {elapsed:.1f}s")
    for msg in (messages or []):
        if not msg or getattr(msg, "empty", False):
            continue
        raw = _raw_media_name(msg)
        if not raw:
            continue
        info = _split_part_info(raw)
        if not info or info[0] != base:
            continue
        media = msg.video or msg.document
        parts[info[1]] = {"msg_id": msg.id, "size_bytes": getattr(media, "file_size", 0) or 0}
    return parts


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
    except Exception as e:
        LOGGER.warning(f"[USERBOT] Could not fetch chat title for {chat_id}: {e}")
        title = str(chat_id)
    _chat_title_cache[chat_id] = title
    return title


def _strip_symbols(text: str) -> str:
    if not text:
        return ""
    text = _APOSTROPHE_RE.sub("", text)
    text = _SYMBOL_STRIP_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _absolute_ep_forms(episode: int) -> List[str]:
    ep = int(episode)
    if ep < 1:
        return [str(ep)]
    if ep < 10:
        return [f"{ep:03d}", f"{ep:02d}", str(ep)]
    if ep < 100:
        return [f"{ep:03d}", str(ep)]
    return [str(ep)]


def _build_search_query(expected_title: str, year: Optional[int], season: Optional[int], episode: Optional[int]) -> str:
    if season is not None and episode is not None:
        return f"{expected_title} S{int(season):02d}E{int(episode):02d}"
    if season is None and episode is not None:
        forms = _absolute_ep_forms(int(episode))
        return f"{expected_title} {forms[0]}"
    if year is not None:
        return f"{expected_title} {year}"
    return expected_title


def _build_query_candidates(
    expected_title: str, year: Optional[int], season: Optional[int], episode: Optional[int]
) -> List[str]:
    candidates: List[str] = []

    def add(q: Optional[str]) -> None:
        q = (q or "").strip()
        if q and q.lower() not in (c.lower() for c in candidates):
            candidates.append(q)

    if season is None and episode is not None:
        titles = [expected_title]
        stripped_title = _strip_symbols(expected_title)
        if stripped_title and stripped_title.lower() != expected_title.lower():
            titles.append(stripped_title)
        for form in _absolute_ep_forms(int(episode)):
            for title in titles:
                add(f"{title} {form}")
                add(f"{title} E{form}")
        return candidates

    add(_build_search_query(expected_title, year, season, episode))

    if season is None and episode is None and year is not None:
        add(expected_title)

    stripped_title = _strip_symbols(expected_title)
    if stripped_title and stripped_title.lower() != expected_title.lower():
        add(_build_search_query(stripped_title, year, season, episode))
        if season is None and episode is None and year is not None:
            add(stripped_title)

    return candidates


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

    async with _channel_semaphore:
        results: List[Dict] = []
        split_groups: Dict[str, dict] = {}
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

                    raw_name = _raw_media_name(message)
                    if not raw_name:
                        continue

                    split = _split_part_info(raw_name)
                    if split:
                        base, _part_num, display, is_zip = split
                        if base in split_groups:
                            continue
                        parsed = _validate_name(display, expected_title, season, episode)
                        if parsed is None:
                            continue
                        parts = await _gather_split_parts(client, chat_id, message.id, base)
                        if len(parts) < 2:
                            continue
                        split_groups[base] = {
                            "parts": parts,
                            "display": display,
                            "quality": parsed.get("resolution") or "HD",
                            "zip": is_zip,
                        }
                        continue

                    filename = _video_filename(message)
                    if not filename:
                        continue
                    parsed = _parse_and_validate(filename, expected_title, season, episode)
                    if parsed is None:
                        continue

                    media = message.video or message.document
                    size = get_readable_file_size(getattr(media, "file_size", 0) or 0)
                    quality = parsed.get("resolution") or "HD"

                    is_single_zip = bool(filename and filename.lower().endswith(".zip"))
                    payload = {
                        "global": True,
                        "chat_id": chat_id,
                        "msg_id": message.id,
                        "title": filename,
                        "size": size,
                        "quality": quality,
                        "source": chat_title,
                    }
                    if is_single_zip:
                        # Represent as a one-part zip so stream routes use the zip path
                        payload["zip"] = True
                        payload["parts"] = [{"chat_id": chat_id, "msg_id": message.id}]
                        del payload["chat_id"]
                        del payload["msg_id"]
                    token = await encode_string(payload)

                    results.append({
                        "token": token,
                        "title": filename,
                        "size": size,
                        "source_chat": chat_title,
                        "quality": quality,
                        "is_zip": is_single_zip,
                    })
                    LOGGER.debug(f"[GLOBAL SEARCH] Result found: {filename} in {chat_title}")

                    if len(results) >= MAX_RESULTS_PER_CHAT:
                        break

            except FloodWait as e:
                LOGGER.warning(f"[USERBOT] FloodWait for {chat_title}: sleeping {e.value}s")
                await asyncio.sleep(e.value)
            except (ChatAdminRequired, ChannelPrivate, PeerIdInvalid, UserNotParticipant, RPCError) as e:
                LOGGER.warning(f"[USERBOT] Cannot access channel {chat_title} ({chat_id}): {e}")
                break
            except (AuthKeyUnregistered, SessionRevoked) as e:
                LOGGER.error(f"[USERBOT] Session invalid ({type(e).__name__}): {e}")
                _userbot_session_dead = True
                break
            except Exception as e:
                LOGGER.warning(f"[USERBOT] Unexpected error searching {chat_title} ({chat_id}): {e}")
                break

        for group in split_groups.values():
            ordered = [group["parts"][pn] for pn in sorted(group["parts"])]
            total_bytes = sum(p["size_bytes"] for p in ordered)
            size = get_readable_file_size(total_bytes)
            is_zip = bool(group.get("zip"))
            payload = {
                "global": True,
                "parts": [{"chat_id": chat_id, "msg_id": p["msg_id"]} for p in ordered],
                "title": group["display"],
                "size": size,
                "quality": group["quality"],
                "source": chat_title,
            }
            if is_zip:
                payload["zip"] = True
            token = await encode_string(payload)
            results.append({
                "token": token,
                "title": group["display"],
                "size": size,
                "source_chat": chat_title,
                "quality": group["quality"],
                "is_split": True,
                "is_zip": is_zip,
                "part_count": len(ordered),
            })
            kind = "zip parts" if is_zip else "parts"
            LOGGER.info(f"[GLOBAL SEARCH] Split stream: {group['display']} ({len(ordered)} {kind}) in {chat_title}")

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

    query_candidates = _build_query_candidates(expected_title, year, season, episode)
    if not query_candidates:
        return []

    key = query_candidates[0].lower()
    now = time.time()

    existing_task = _inflight_tasks.get(key)
    if existing_task and not existing_task.done():
        LOGGER.info(f"[GLOBAL SEARCH] Joining in-flight search for '{query_candidates[0]}'")
        try:
            return await existing_task
        except Exception:
            return []

    cached = _result_cache.get(key)
    if cached is not None and (now - cached[0]) < RESULT_CACHE_SECONDS:
        LOGGER.info(f"[GLOBAL SEARCH] Serving cached results for '{query_candidates[0]}'")
        return cached[1]

    if now - _last_search_ts.get(key, 0) < SEARCH_COOLDOWN_SECONDS:
        if cached is not None:
            LOGGER.info(f"[GLOBAL SEARCH] Serving cached results for '{query_candidates[0]}' (cooldown)")
            return cached[1]
        LOGGER.info(f"[GLOBAL SEARCH] Cooldown active for '{query_candidates[0]}'")
        return []

    _last_search_ts[key] = now
    if target_ids:
        task = asyncio.create_task(
            _run_global_search(expected_title, query_candidates, target_ids, season, episode)
        )
    else:
        task = asyncio.create_task(
            _run_true_global_search(expected_title, query_candidates, season, episode)
        )
    _inflight_tasks[key] = task
    try:
        results = await task
        _result_cache[key] = (time.time(), results)
        return results
    finally:
        _inflight_tasks.pop(key, None)


async def _run_global_search(
    expected_title: str,
    query_candidates: List[str],
    target_ids: List[int],
    season: Optional[int],
    episode: Optional[int],
) -> List[Dict]:
    async with _search_semaphore:
        chat_titles = await asyncio.gather(
            *(_get_chat_title(botmod.Userbot, cid) for cid in target_ids),
            return_exceptions=True,
        )
        resolved_titles = [
            str(cid) if isinstance(t, Exception) else t
            for cid, t in zip(target_ids, chat_titles)
        ]

        all_results: List[Dict] = []
        for attempt_idx, search_query in enumerate(query_candidates):
            if _userbot_session_dead:
                break

            LOGGER.info(
                f"[USERBOT] Search attempt {attempt_idx + 1}/{len(query_candidates)}: "
                f"'{search_query}' across {len(target_ids)} channel(s)"
            )

            search_tasks = [
                _search_channel(botmod.Userbot, int(cid), title, search_query, expected_title, season, episode)
                for cid, title in zip(target_ids, resolved_titles)
            ]
            per_channel_results = await asyncio.gather(*search_tasks, return_exceptions=True)

            for r in per_channel_results:
                if isinstance(r, list):
                    all_results.extend(r)

            if all_results:
                LOGGER.info(
                    f"[USERBOT] Succeeded on attempt {attempt_idx + 1} "
                    f"('{search_query}') -> {len(all_results)} result(s)"
                )
                break
            if attempt_idx + 1 < len(query_candidates):
                LOGGER.info(f"[USERBOT] No results for '{search_query}', trying next fallback")

        all_results = all_results[:MAX_RESULTS]
        LOGGER.info(f"[USERBOT] Search completed: '{expected_title}' -> {len(all_results)} result(s)")
        return all_results


async def _run_true_global_search(
    expected_title: str,
    query_candidates: List[str],
    season: Optional[int],
    episode: Optional[int],
) -> List[Dict]:
    global _userbot_session_dead
    async with _search_semaphore:
        all_results: List[Dict] = []
        split_groups: Dict[str, dict] = {}
        seen_msg_ids: set = set()

        for attempt_idx, search_query in enumerate(query_candidates):
            if _userbot_session_dead:
                break

            LOGGER.info(
                f"[USERBOT] True global search attempt {attempt_idx + 1}/{len(query_candidates)}: '{search_query}'"
            )

            for msg_filter in (enums.MessagesFilter.VIDEO, enums.MessagesFilter.DOCUMENT):
                if len(all_results) + len(split_groups) >= MAX_RESULTS:
                    break
                try:
                    async for message in botmod.Userbot.search_global(
                        query=search_query,
                        filter=msg_filter,
                        channels_only=True,
                        limit=MAX_RESULTS,
                    ):
                        if not message or not message.chat:
                            continue
                        chat = message.chat
                        if chat.type != enums.ChatType.CHANNEL:
                            continue
                        chat_id = chat.id
                        chat_title = chat.title or str(chat_id)
                        _chat_title_cache[chat_id] = chat_title

                        msg_key = (chat_id, message.id)
                        if msg_key in seen_msg_ids:
                            continue
                        seen_msg_ids.add(msg_key)

                        raw_name = _raw_media_name(message)
                        if not raw_name:
                            continue

                        split = _split_part_info(raw_name)
                        if split:
                            base, _part_num, display, is_zip = split
                            group_key = f"{chat_id}:{base}"
                            if group_key in split_groups:
                                continue
                            parsed = _validate_name(display, expected_title, season, episode)
                            if parsed is None:
                                continue
                            parts = await _gather_split_parts(botmod.Userbot, chat_id, message.id, base)
                            if len(parts) < 2:
                                continue
                            split_groups[group_key] = {
                                "chat_id": chat_id,
                                "chat_title": chat_title,
                                "parts": parts,
                                "display": display,
                                "quality": parsed.get("resolution") or "HD",
                                "zip": is_zip,
                            }
                            continue

                        filename = _video_filename(message)
                        if not filename:
                            continue
                        parsed = _parse_and_validate(filename, expected_title, season, episode)
                        if parsed is None:
                            continue

                        media = message.video or message.document
                        size = get_readable_file_size(getattr(media, "file_size", 0) or 0)
                        quality = parsed.get("resolution") or "HD"

                        is_single_zip = bool(filename and filename.lower().endswith(".zip"))
                        payload = {
                            "global": True,
                            "chat_id": chat_id,
                            "msg_id": message.id,
                            "title": filename,
                            "size": size,
                            "quality": quality,
                            "source": chat_title,
                        }
                        if is_single_zip:
                            payload["zip"] = True
                            payload["parts"] = [{"chat_id": chat_id, "msg_id": message.id}]
                            del payload["chat_id"]
                            del payload["msg_id"]
                        token = await encode_string(payload)

                        all_results.append({
                            "token": token,
                            "title": filename,
                            "size": size,
                            "source_chat": chat_title,
                            "quality": quality,
                            "is_zip": is_single_zip,
                        })
                        LOGGER.debug(f"[GLOBAL SEARCH] True global result: {filename} in {chat_title}")

                        if len(all_results) >= MAX_RESULTS:
                            break

                except FloodWait as e:
                    LOGGER.warning(f"[USERBOT] FloodWait on true global: sleeping {e.value}s")
                    await asyncio.sleep(e.value)
                except (AuthKeyUnregistered, SessionRevoked) as e:
                    LOGGER.error(f"[USERBOT] Session invalid ({type(e).__name__}): {e}")
                    _userbot_session_dead = True
                    break
                except Exception as e:
                    LOGGER.warning(f"[USERBOT] True global search error: {e}")
                    break

            if all_results or split_groups:
                LOGGER.info(
                    f"[USERBOT] True global succeeded on attempt {attempt_idx + 1} "
                    f"('{search_query}') -> {len(all_results)} + {len(split_groups)} split group(s)"
                )
                break
            if attempt_idx + 1 < len(query_candidates):
                LOGGER.info(f"[USERBOT] No results for '{search_query}', trying next fallback")

        for group in split_groups.values():
            chat_id = group["chat_id"]
            chat_title = group["chat_title"]
            ordered = [group["parts"][pn] for pn in sorted(group["parts"])]
            total_bytes = sum(p["size_bytes"] for p in ordered)
            size = get_readable_file_size(total_bytes)
            is_zip = bool(group.get("zip"))
            payload = {
                "global": True,
                "parts": [{"chat_id": chat_id, "msg_id": p["msg_id"]} for p in ordered],
                "title": group["display"],
                "size": size,
                "quality": group["quality"],
                "source": chat_title,
            }
            if is_zip:
                payload["zip"] = True
            token = await encode_string(payload)
            all_results.append({
                "token": token,
                "title": group["display"],
                "size": size,
                "source_chat": chat_title,
                "quality": group["quality"],
                "is_split": True,
                "is_zip": is_zip,
                "part_count": len(ordered),
            })
            kind = "zip parts" if is_zip else "parts"
            LOGGER.info(f"[GLOBAL SEARCH] Split stream: {group['display']} ({len(ordered)} {kind}) in {chat_title}")

        all_results = all_results[:MAX_RESULTS]
        LOGGER.info(f"[USERBOT] True global search completed: '{expected_title}' -> {len(all_results)} result(s)")
        return all_results
