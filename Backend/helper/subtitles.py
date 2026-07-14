import re
from datetime import datetime

from Backend import db
from Backend.helper.encrypt import encode_string
from Backend.helper.manual_add import parse_telegram_link
from Backend.helper.metadata import (
    extract_default_id,
    fetch_movie_metadata,
    fetch_tv_metadata,
    parse_media_name,
)
from Backend.helper.pyro import clean_filename
from Backend.logger import LOGGER

SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa", ".sub")

#----- (ISO 639-2 code, label, match aliases: full names + ISO 639-2/639-1 codes)
_LANGUAGES = [
    ("eng", "English", ("english", "eng", "en")),
    ("hin", "Hindi", ("hindi", "hin", "hi")),
    ("tam", "Tamil", ("tamil", "tam", "ta")),
    ("tel", "Telugu", ("telugu", "tel", "te")),
    ("kan", "Kannada", ("kannada", "kan", "kn")),
    ("mal", "Malayalam", ("malayalam", "mal", "ml")),
    ("ben", "Bengali", ("bengali", "bangla", "ben", "bn")),
    ("mar", "Marathi", ("marathi", "mar", "mr")),
    ("pan", "Punjabi", ("punjabi", "panjabi", "pan", "pa")),
    ("guj", "Gujarati", ("gujarati", "guj", "gu")),
    ("urd", "Urdu", ("urdu", "urd", "ur")),
    ("ori", "Odia", ("odia", "oriya", "ori", "or")),
    ("asm", "Assamese", ("assamese", "asm", "as")),
    ("bho", "Bhojpuri", ("bhojpuri", "bho")),
    ("kok", "Konkani", ("konkani", "kok")),
    ("nep", "Nepali", ("nepali", "nep", "ne")),
    ("sin", "Sinhala", ("sinhala", "sinhalese", "sin", "si")),
    ("san", "Sanskrit", ("sanskrit", "san", "sa")),
    ("spa", "Spanish", ("spanish", "espanol", "spa", "es")),
    ("fre", "French", ("french", "francais", "fre", "fra", "fr")),
    ("ger", "German", ("german", "deutsch", "ger", "deu", "de")),
    ("ita", "Italian", ("italian", "italiano", "ita", "it")),
    ("por", "Portuguese", ("portuguese", "portugues", "por", "pt")),
    ("rus", "Russian", ("russian", "rus", "ru")),
    ("ara", "Arabic", ("arabic", "ara", "ar")),
    ("jpn", "Japanese", ("japanese", "jpn", "ja")),
    ("kor", "Korean", ("korean", "kor", "ko")),
    ("chi", "Chinese", ("chinese", "mandarin", "cantonese", "chi", "zho", "zh")),
    ("tha", "Thai", ("thai", "tha", "th")),
    ("vie", "Vietnamese", ("vietnamese", "vie", "vi")),
    ("ind", "Indonesian", ("indonesian", "bahasa", "ind", "id")),
    ("may", "Malay", ("malay", "melayu", "msa", "ms")),
    ("fil", "Filipino", ("filipino", "tagalog", "fil", "tgl", "tl")),
    ("tur", "Turkish", ("turkish", "turkce", "tur", "tr")),
    ("dut", "Dutch", ("dutch", "nederlands", "dut", "nld", "nl")),
    ("pol", "Polish", ("polish", "polski", "pol", "pl")),
    ("swe", "Swedish", ("swedish", "svenska", "swe", "sv")),
    ("nor", "Norwegian", ("norwegian", "norsk", "nor", "no")),
    ("dan", "Danish", ("danish", "dansk", "dan", "da")),
    ("fin", "Finnish", ("finnish", "suomi", "fin", "fi")),
    ("gre", "Greek", ("greek", "gre", "ell", "el")),
    ("heb", "Hebrew", ("hebrew", "heb", "he")),
    ("per", "Persian", ("persian", "farsi", "per", "fas", "fa")),
    ("ukr", "Ukrainian", ("ukrainian", "ukr", "uk")),
    ("rum", "Romanian", ("romanian", "rum", "ron", "ro")),
    ("hun", "Hungarian", ("hungarian", "magyar", "hun", "hu")),
    ("cze", "Czech", ("czech", "cesky", "cze", "ces", "cs")),
    ("swa", "Swahili", ("swahili", "swa", "sw")),
]


def is_subtitle_file(name: str) -> bool:
    return bool(name) and name.lower().strip().endswith(SUBTITLE_EXTS)


def subtitle_ext(name: str) -> str:
    low = (name or "").lower()
    for ext in SUBTITLE_EXTS:
        if low.endswith(ext):
            return ext
    return ".srt"


#----- token -> (code, label) for exact matches; full-name set for trimming
_LANG_BY_TOKEN = {}
_LANG_WORDS = set()
for _code, _label, _aliases in _LANGUAGES:
    for _alias in _aliases:
        _LANG_BY_TOKEN.setdefault(_alias, (_code, _label))
        if len(_alias) >= 4:
            _LANG_WORDS.add(_alias)
    _LANG_WORDS.add(_label.lower())

_SUB_EXT_TOKENS = {ext.lstrip(".") for ext in SUBTITLE_EXTS}

#----- Trailing tokens that describe the subtitle, not its language
_IGNORE_TOKENS = {"forced", "sdh", "cc", "full", "default", "hearing", "impaired",
                  "dubbed", "dub", "sub", "subs", "subtitle", "subtitles"}

_TRAILING_LANG_RE = re.compile(
    r"[\s._-]+(" + "|".join(sorted(_LANG_WORDS, key=len, reverse=True)) + r")\s*$",
    re.IGNORECASE,
)


#----- Detect language from filename tokens: full names first, then ISO codes, scanning from the end
def detect_language(name: str):
    tokens = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t and t not in _SUB_EXT_TOKENS]
    while tokens and tokens[-1] in _IGNORE_TOKENS:
        tokens.pop()
    for token in reversed(tokens):
        if len(token) >= 4 and token in _LANG_BY_TOKEN:
            return _LANG_BY_TOKEN[token]
    for token in reversed(tokens):
        if len(token) == 3 and token in _LANG_BY_TOKEN:
            return _LANG_BY_TOKEN[token]
    if tokens and len(tokens[-1]) == 2 and tokens[-1] in _LANG_BY_TOKEN:
        return _LANG_BY_TOKEN[tokens[-1]]
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


#----- Match, tag and store a subtitle; returns True when stored, False when skipped
async def ingest_subtitle(name: str, channel: int, msg_id: int) -> bool:
    try:
        identified = await _identify(name)
        if not identified:
            LOGGER.info(f"[SUBTITLE] Could not match '{name}'")
            return False
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
                "source": "auto",
                "added_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        LOGGER.info(f"[SUBTITLE] Stored {label} for {imdb_id} (S{season}E{episode}): {name}")
        return True
    except Exception as e:
        LOGGER.error(f"[SUBTITLE] ingest failed for '{name}': {e}")
        return False


def list_languages() -> list:
    return [{"code": code, "label": label} for code, label, _ in _LANGUAGES]


def _label_for(code: str) -> str:
    code = (code or "und").lower()
    return next((label for c, label, _ in _LANGUAGES if c == code), "Unknown")


async def resolve_subtitle_message(client, url: str = None, chat_id=None, msg_id=None) -> dict:
    if url:
        chat_ref, msg_id = parse_telegram_link(url)
        if chat_ref is None:
            raise ValueError("Could not read that Telegram link. Use a t.me/c/... or t.me/<channel>/... message link.")
    elif chat_id and msg_id:
        chat_ref = int(f"-100{str(chat_id).replace('-100', '')}")
        msg_id = int(msg_id)
    else:
        raise ValueError("Provide a Telegram message link, or a chat id and message id.")

    message = await client.get_messages(chat_ref, msg_id)
    if not message or getattr(message, "empty", False):
        raise ValueError("That message was not found. Make sure the bot is in the channel.")

    doc = getattr(message, "document", None)
    fname = getattr(doc, "file_name", None) if doc else None
    if not fname or not is_subtitle_file(fname):
        raise ValueError("That message has no subtitle file (.srt, .vtt, .ass, .ssa, .sub).")

    code, label = detect_language(fname)
    return {
        "chat_id": str(message.chat.id).replace("-100", ""),
        "msg_id": message.id,
        "name": fname,
        "ext": subtitle_ext(fname),
        "lang_code": code,
        "lang_label": label,
    }


async def manual_ingest_subtitle(imdb_id, media_type, season, episode, lang_code, chat_id, msg_id, name) -> dict:
    channel = int(str(chat_id).replace("-100", ""))
    msg_id = int(msg_id)
    code = (lang_code or "und").lower()
    doc = {
        "imdb_id": imdb_id,
        "media_type": "tv" if media_type in ("tv", "series") else "movie",
        "season": int(season) if season else None,
        "episode": int(episode) if episode else None,
        "lang_code": code,
        "lang_label": _label_for(code),
        "name": name,
        "chat_id": channel,
        "msg_id": msg_id,
        "encoded": await encode_string({"chat_id": channel, "msg_id": msg_id}),
        "source": "manual",
        "added_at": datetime.utcnow(),
    }
    await db.dbs["tracking"]["subtitles"].update_one(
        {"chat_id": channel, "msg_id": msg_id}, {"$set": doc}, upsert=True
    )
    return doc


async def list_title_subtitles(imdb_id: str) -> list:
    out = []
    cursor = db.dbs["tracking"]["subtitles"].find({"imdb_id": imdb_id}).sort(
        [("season", 1), ("episode", 1), ("lang_label", 1)]
    )
    async for doc in cursor:
        doc["_id"] = str(doc.get("_id"))
        doc.pop("encoded", None)
        out.append(doc)
    return out


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
    lang_counts = {}
    for sub in subs:
        key = sub.get("lang_label") or sub.get("lang_code") or "Unknown"
        lang_counts[key] = lang_counts.get(key, 0) + 1

    seen = {}
    entries = []
    for sub in subs:
        ext = subtitle_ext(sub.get("name"))
        base_label = sub.get("lang_label") or sub.get("lang_code") or "Unknown"
        if lang_counts.get(base_label, 0) > 1:
            seen[base_label] = seen.get(base_label, 0) + 1
            lang = f"{base_label} ({seen[base_label]})"
        else:
            lang = base_label
        entries.append({
            "id": f"tg-{sub.get('msg_id')}",
            "url": f"{base_url}/sub/{token}/{sub.get('encoded')}/subtitle{ext}",
            "lang": lang,
        })
    return entries
