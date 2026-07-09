import re
from datetime import datetime

from bson import ObjectId

from Backend import __version__, db
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

#----- Never export/restore credentials or host-specific database topology
_SETTINGS_EXCLUDE = {"admin_password", "session_secret", "extra_databases"}

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

    #----- Settings: merge importable keys, preserve password/secret/host DBs
    settings = payload.get("settings")
    if isinstance(settings, dict):
        clean = {k: v for k, v in settings.items() if k not in _SETTINGS_EXCLUDE and k != "_id"}
        if clean:
            await db.save_settings({**SettingsManager.current().to_dict(), **clean})
            await SettingsManager.reload(db)
        result["settings"] = f"{len(clean)} keys"

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
