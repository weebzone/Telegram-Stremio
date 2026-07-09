import re
from datetime import datetime

from Backend import db
from Backend.helper.encrypt import encode_string
from Backend.helper.metadata import (
    extract_default_id,
    fetch_movie_metadata,
    fetch_tv_metadata,
    parse_media_name,
)
from Backend.helper.pyro import clean_filename
from Backend.logger import LOGGER

SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")

#----- filename hint -> (ISO 639-2 code, label)
_LANG_PATTERNS = [
    (("english", "eng", ".en."), ("eng", "English")),
    (("hindi", "hin", ".hi."), ("hin", "Hindi")),
    (("spanish", "espanol", "spa", ".es."), ("spa", "Spanish")),
    (("french", "francais", "fre", "fra", ".fr."), ("fre", "French")),
    (("german", "deu", "ger", ".de."), ("ger", "German")),
    (("italian", "ita", ".it."), ("ita", "Italian")),
    (("arabic", "ara", ".ar."), ("ara", "Arabic")),
    (("portuguese", "por", ".pt."), ("por", "Portuguese")),
    (("russian", "rus", ".ru."), ("rus", "Russian")),
    (("japanese", "jpn", ".ja."), ("jpn", "Japanese")),
    (("korean", "kor", ".ko."), ("kor", "Korean")),
    (("chinese", "zho", "chi", ".zh."), ("chi", "Chinese")),
    (("tamil", "tam", ".ta."), ("tam", "Tamil")),
    (("telugu", "tel", ".te."), ("tel", "Telugu")),
    (("malayalam", "mal", ".ml."), ("mal", "Malayalam")),
    (("bengali", "ben", ".bn."), ("ben", "Bengali")),
]


def is_subtitle_file(name: str) -> bool:
    return bool(name) and name.lower().strip().endswith(SUBTITLE_EXTS)


def subtitle_ext(name: str) -> str:
    low = (name or "").lower()
    for ext in SUBTITLE_EXTS:
        if low.endswith(ext):
            return ext
    return ".srt"


#----- Full-word language names (len >= 4) used to trim a trailing language token
_LANG_WORDS = set()
for _needles, (_code, _label) in _LANG_PATTERNS:
    _LANG_WORDS.add(_label.lower())
    for _n in _needles:
        _n = _n.strip(".")
        if len(_n) >= 4:
            _LANG_WORDS.add(_n)

_TRAILING_LANG_RE = re.compile(
    r"[\s._-]+(" + "|".join(sorted(_LANG_WORDS, key=len, reverse=True)) + r")\s*$",
    re.IGNORECASE,
)


def detect_language(name: str):
    low = f".{(name or '').lower()}."
    for needles, result in _LANG_PATTERNS:
        if any(n in low for n in needles):
            return result
    return "und", "Unknown"


#----- Drop the subtitle extension and any trailing language word(s)
def _strip_language(name: str) -> str:
    base = name or ""
    for ext in SUBTITLE_EXTS:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    prev = None
    while prev != base:
        prev = base
        base = _TRAILING_LANG_RE.sub("", base)
    return base.strip() or (name or "")


#----- Resolve a subtitle filename to (imdb_id, media_type, season, episode)
async def _identify(name: str):
    default_id = extract_default_id(name)
    parsed = parse_media_name(clean_filename(_strip_language(name)))
    title = parsed.get("title")
    year = parsed.get("year")
    season = parsed.get("season")
    episode = parsed.get("episode")

    #----- Need either a direct IMDb/TMDb id or a parsable title to match
    if not default_id and not title:
        return None

    is_tv = bool(season and episode and not isinstance(season, list) and not isinstance(episode, list))
    if is_tv:
        info = await fetch_tv_metadata(title or "", int(season), int(episode), None, year, None, default_id)
        season_out, episode_out = int(season), int(episode)
    else:
        info = await fetch_movie_metadata(title or "", None, year, None, default_id)
        season_out = episode_out = None

    if not info or not info.get("imdb_id"):
        return None
    media_type = "tv" if info.get("media_type") in ("tv", "series") else "movie"
    return info["imdb_id"], media_type, season_out, episode_out


async def ingest_subtitle(name: str, channel: int, msg_id: int) -> None:
    try:
        identified = await _identify(name)
        if not identified:
            LOGGER.info(f"[SUBTITLE] Could not match '{name}'")
            return
        imdb_id, media_type, season, episode = identified
        code, label = detect_language(name)
        encoded = await encode_string({"chat_id": channel, "msg_id": msg_id})

        await db.dbs["tracking"]["subtitles"].update_one(
            {"chat_id": int(channel), "msg_id": int(msg_id)},
            {"$set": {
                "imdb_id": imdb_id,
                "media_type": media_type,
                "season": int(season) if season else None,
                "episode": int(episode) if episode else None,
                "lang_code": code,
                "lang_label": label,
                "name": name,
                "chat_id": int(channel),
                "msg_id": int(msg_id),
                "encoded": encoded,
                "added_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        LOGGER.info(f"[SUBTITLE] Stored {label} for {imdb_id} (S{season}E{episode}): {name}")
    except Exception as e:
        LOGGER.error(f"[SUBTITLE] ingest failed for '{name}': {e}")


async def get_subtitles_for(imdb_id: str, media_type: str, season, episode):
    query = {"imdb_id": imdb_id}
    if media_type == "tv":
        query["season"] = int(season) if season else None
        query["episode"] = int(episode) if episode else None
    return [doc async for doc in db.dbs["tracking"]["subtitles"].find(query)]


async def remove_subtitle(channel, msg_id) -> bool:
    result = await db.dbs["tracking"]["subtitles"].delete_one(
        {"chat_id": int(channel), "msg_id": int(msg_id)}
    )
    return result.deleted_count > 0


#----- Shape stored subtitles into Stremio subtitle objects
def stremio_subtitle_entries(subs: list, token: str, base_url: str) -> list:
    entries = []
    for sub in subs:
        ext = subtitle_ext(sub.get("name"))
        entries.append({
            "id": f"tg-{sub.get('msg_id')}",
            "url": f"{base_url}/sub/{token}/{sub.get('encoded')}/subtitle{ext}",
            "lang": sub.get("lang_code") or "und",
        })
    return entries
