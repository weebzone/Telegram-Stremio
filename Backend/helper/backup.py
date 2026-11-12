import json
import re
import zlib
from datetime import datetime

from bson import ObjectId

from Backend import __version__, db
from Backend.helper.encrypt import decode_string
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

#----- Never export/restore credentials; everything else (incl. extra DBs and
#----- bot tokens) is included so a backup can fully migrate a deployment.
_SETTINGS_EXCLUDE = {"admin_password", "session_secret"}

#----- backup section -> tracking collection
_COLLECTIONS = {
    "custom_catalogs": "custom_catalogs",
    "subscription_plans": "sub_plans",
    "tokens": "api_tokens",
}

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _jsonify(obj):
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonify(x) for x in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _revive(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "_id" and isinstance(v, str) and len(v) == 24:
                try:
                    out[k] = ObjectId(v)
                    continue
                except Exception:
                    pass
            out[k] = _revive(v)
        return out
    if isinstance(obj, list):
        return [_revive(x) for x in obj]
    if isinstance(obj, str) and _ISO_RE.match(obj):
        try:
            return datetime.fromisoformat(obj.replace("Z", ""))
        except Exception:
            return obj
    return obj


async def export_config() -> dict:
    settings = {k: v for k, v in SettingsManager.current().to_dict().items() if k not in _SETTINGS_EXCLUDE}
    data = {
        "app": "telegram-stremio",
        "version": __version__,
        "exported_at": datetime.utcnow().isoformat(),
        "settings": settings,
    }
    for label, coll in _COLLECTIONS.items():
        data[label] = await db.dbs["tracking"][coll].find({}).to_list(None)
    return _jsonify(data)


async def import_config(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("app") != "telegram-stremio":
        raise ValueError("This doesn't look like a Telegram-Stremio backup file.")

    result = {}

    #----- Settings: apply via SettingsManager.update so extra databases connect
    #----- and multi-token clients start (password/secret are preserved).
    settings = payload.get("settings")
    if isinstance(settings, dict):
        clean = {k: v for k, v in settings.items() if k not in _SETTINGS_EXCLUDE and k != "_id"}
        if clean:
            reinit = await SettingsManager.update(db, clean)
            result["settings"] = f"{len(clean)} keys applied"
            if reinit:
                result["reinit"] = reinit

    #----- Collections: replace with the backup's contents
    for label, coll in _COLLECTIONS.items():
        section = payload.get(label)
        if not isinstance(section, list):
            continue
        docs = [_revive(d) for d in section if isinstance(d, dict)]
        collection = db.dbs["tracking"][coll]
        await collection.delete_many({})
        if docs:
            await collection.insert_many(docs)
        result[label] = f"{len(docs)} restored"

    LOGGER.info(f"[BACKUP] Config restored: {result}")
    return result


#----- Library export in TeleStremio's catalog format (gzip, streamed doc by doc)
_UNITS = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}


def _size_bytes(size) -> int:
    m = re.match(r"([\d.]+)\s*([KMGT]?B)", str(size or "").upper())
    return int(float(m.group(1)) * _UNITS[m.group(2)]) if m else 0


async def _quality(q: dict):
    parts = q.get("parts") or []
    if not parts:
        try:
            ref = await decode_string(q.get("id") or "")
            parts = [{"part_number": 0, "chat_id": ref["chat_id"], "msg_id": ref["msg_id"], "size_bytes": _size_bytes(q.get("size"))}]
        except Exception:
            return None
    parts = [{k: int(p.get(k) or 0) for k in ("part_number", "chat_id", "msg_id", "size_bytes")} for p in parts]
    return {
        "quality": q.get("quality") or "HD", "id": "", "name": q.get("name") or "", "size": q.get("size") or "",
        "size_bytes": sum(p["size_bytes"] for p in parts) or _size_bytes(q.get("size")),
        "group_key": q.get("group_key"), "parts": parts,
    }


async def _qualities(items) -> list:
    return [q for q in [await _quality(i) for i in items or []] if q]


async def _library_doc(doc: dict, kind: str) -> dict:
    d = _jsonify({k: v for k, v in doc.items() if k not in ("_id", "updated_on")})
    if isinstance(doc.get("updated_on"), datetime):
        d["updated_on"] = d["added_on"] = int(doc["updated_on"].timestamp() * 1000)
    if kind == "movie":
        d["telegram"] = await _qualities(doc.get("telegram"))
    else:
        for season in d.get("seasons") or []:
            for ep in season.get("episodes") or []:
                ep["telegram"] = await _qualities(ep.get("telegram"))
    return d


async def export_library():
    s = SettingsManager.current().to_dict()
    anime = {str(c) for c in s.get("anime_channels") or []}
    titles = s.get("channel_titles") or {}
    gz = zlib.compressobj(6, zlib.DEFLATED, 31)
    yield gz.compress(b'{"format":"telestremio-catalog","version":2,"source":"telegram-stremio"')
    for kind in ("movie", "tv"):
        yield gz.compress(f',"{kind}":['.encode())
        sep = ""
        for key, store in db.dbs.items():
            if not key.startswith("storage_"):
                continue
            async for doc in store[kind].find({}):
                yield gz.compress((sep + json.dumps(await _library_doc(doc, kind))).encode())
                sep = ","
        yield gz.compress(b"]")
    channels = [
        {"ref": str(c), "anime": str(c) in anime, "title": titles.get(str(c), ""),
         "chat_id": int(c) if str(c).lstrip("-").isdigit() else 0}
        for c in s.get("auth_channels") or []
    ]
    yield gz.compress(f',"channels":{json.dumps(channels)}}}'.encode())
    yield gz.flush()
