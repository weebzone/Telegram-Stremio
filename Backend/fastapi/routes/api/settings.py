"""
api/settings.py — application settings get/update.
"""


from fastapi import HTTPException

from Backend import db
from Backend.helper.security.passwords import hash_password
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


async def get_settings_api() -> dict:

    data = SettingsManager.current().to_dict()
    data["admin_password_set"] = bool(data.get("admin_password"))
    data["admin_password"] = ""
    data["session_secret_set"] = bool(data.get("session_secret"))
    data["session_secret"] = ""

    try:
        data["database_list"] = db.get_database_list()
    except Exception as e:
        LOGGER.error(f"get_settings_api: could not load database list: {e}")
        data["database_list"] = []

    active = SettingsManager._all_channel_ids(data)
    titles = data.get("channel_titles") or {}
    if not isinstance(titles, dict):
        titles = {}
    titles = {str(k): str(v) for k, v in titles.items() if str(k) in active and v}
    missing = [cid for cid in active if cid not in titles]
    if missing:
        full = SettingsManager.current().to_dict()
        await SettingsManager._sync_channel_titles(full)
        try:
            await db.save_settings(full)
            SettingsManager._current = SettingsManager.current().__class__(full)
        except Exception as e:
            LOGGER.warning(f"get_settings_api: could not persist channel titles: {e}")
        titles = full.get("channel_titles") or {}
    data["channel_titles"] = {str(k): str(v) for k, v in (titles or {}).items() if k and v}

    return {"settings": data}

async def update_settings_api(payload: dict) -> dict:

    if "admin_password" in payload and not str(payload["admin_password"]).strip():
        del payload["admin_password"]
    if "session_secret" in payload and not str(payload["session_secret"]).strip():
        del payload["session_secret"]

    bool_keys = {"replace_mode", "duplicate_protection", "hide_catalog", "subscription", "show_proxy_and_non_proxy_both", "mediaflow_proxy", "announce_new_content", "delete_on_metadata_fail", "better_poster_enabled", "rpdb_enabled", "fanart_enabled", "fanart_shuffle", "fanart_low_res_poster"}
    for key in bool_keys:
        if key in payload:
            payload[key] = bool(payload[key])

    for key in ("metadata_bot_token", "stream_name_template", "stream_title_template"):
        if key in payload:
            payload[key] = str(payload[key] or "").strip()

    if "ffprobe_max_mb" in payload:
        try:
            payload["ffprobe_max_mb"] = max(1.0, float(payload["ffprobe_max_mb"]))
        except (TypeError, ValueError):
            payload["ffprobe_max_mb"] = 8.0

    list_str_keys = {"auth_channels", "multi_tokens", "extra_databases", "global_search_channels", "anime_channels", "manual_channels"}
    for key in list_str_keys:
        if key in payload:
            if not isinstance(payload[key], list):
                raise HTTPException(status_code=400, detail=f"'{key}' must be a list.")
            payload[key] = [str(v).strip() for v in payload[key] if str(v).strip()]

    if "better_poster" in payload:
        payload["better_poster"] = str(payload["better_poster"] or "").strip()
        if payload["better_poster"] and "{imdb_id}" not in payload["better_poster"]:
            raise HTTPException(status_code=400, detail="wrong betterposter url")

    if "rpdb_api_key" in payload:
        payload["rpdb_api_key"] = str(payload["rpdb_api_key"] or "").strip()

    if "fanart_api_key" in payload:
        payload["fanart_api_key"] = str(payload["fanart_api_key"] or "").strip()

    if "fanart_shuffle_interval" in payload:
        try:
            payload["fanart_shuffle_interval"] = max(0, int(payload["fanart_shuffle_interval"]))
        except (ValueError, TypeError):
            payload["fanart_shuffle_interval"] = 5

    if len([k for k in ("better_poster_enabled", "rpdb_enabled", "fanart_enabled") if payload.get(k)]) > 1:
        raise HTTPException(status_code=400, detail="Enable only one poster provider at a time")

    if payload.get("fanart_enabled") and not str(payload.get("fanart_api_key") or "").strip():
        raise HTTPException(status_code=400, detail="Fanart.tv API key is required")

    if "extra_databases" in payload:
        for uri in payload["extra_databases"]:
            if not uri.startswith(("mongodb://", "mongodb+srv://")):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid database URI (must start with mongodb:// or mongodb+srv://): {uri[:30]}…"
                )

    if "approver_ids" in payload:
        if not isinstance(payload["approver_ids"], list):
            raise HTTPException(status_code=400, detail="'approver_ids' must be a list.")
        try:
            payload["approver_ids"] = [int(v) for v in payload["approver_ids"] if str(v).strip()]
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="All approver_ids must be integers.")

    if "subscription_group_id" in payload:
        try:
            payload["subscription_group_id"] = int(payload["subscription_group_id"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="'subscription_group_id' must be an integer.")
    def _validate_channel_id(channel: str, field: str) -> str:
        channel = str(channel).strip()
        if not channel:
            return ""
        if not channel.startswith("-100") or not channel[4:].isdigit() or len(channel) < 8:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {field}: '{channel}'. Only channel IDs in -100xxxxxxxxxx format are accepted (channels only, no groups/users/bots)."
            )
        return channel

    if "auth_channels" in payload:
        cleaned = []
        for channel in payload["auth_channels"]:
            c = _validate_channel_id(channel, "auth channel")
            if c:
                cleaned.append(c)
        payload["auth_channels"] = cleaned

    if "global_search_channels" in payload:
        cleaned = []
        for channel in payload["global_search_channels"]:
            c = _validate_channel_id(channel, "global search channel")
            if c:
                cleaned.append(c)
        payload["global_search_channels"] = cleaned

    if "anime_channels" in payload:
        cleaned = []
        for channel in payload["anime_channels"]:
            c = _validate_channel_id(channel, "anime channel")
            if c:
                cleaned.append(c)
        payload["anime_channels"] = cleaned

    if "manual_channels" in payload:
        cleaned = []
        for channel in payload["manual_channels"]:
            c = _validate_channel_id(channel, "manual channel")
            if c:
                cleaned.append(c)
        payload["manual_channels"] = cleaned

    if "announcement_channel" in payload and payload["announcement_channel"]:
        payload["announcement_channel"] = _validate_channel_id(
            payload["announcement_channel"], "announcement channel"
        )

    if "skip_channel" in payload and payload["skip_channel"]:
        payload["skip_channel"] = _validate_channel_id(
            payload["skip_channel"], "skip channel"
        )

    _channel_fields = ("auth_channels", "manual_channels", "global_search_channels",
                       "anime_channels", "announcement_channel", "skip_channel")
    if any(field in payload for field in _channel_fields):
        current = SettingsManager.current()

        def _norm_ids(values) -> set:
            if isinstance(values, str):
                values = [values]
            return {str(c).strip().replace("-100", "") for c in (values or []) if str(c).strip()}

        groups = {
            "AUTH": _norm_ids(payload.get("auth_channels", list(current.auth_channels))),
            "MANUAL": _norm_ids(payload.get("manual_channels", list(current.manual_channels))),
            "GLOBAL SEARCH": _norm_ids(payload.get("global_search_channels", list(current.global_search_channels))),
            "ANIME": _norm_ids(payload.get("anime_channels", list(current.anime_channels))),
            "ANNOUNCEMENT": _norm_ids(payload.get("announcement_channel", current.announcement_channel)),
            "SKIP": _norm_ids(payload.get("skip_channel", current.skip_channel)),
        }

        allowed_overlap = frozenset({"AUTH", "ANIME"})
        names = list(groups)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                if frozenset({a, b}) == allowed_overlap:
                    continue
                clash = groups[a] & groups[b]
                if clash:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Channel {', '.join(sorted(clash))} can't be in both {a} and {b} channels — each channel may only belong to one field."
                    )

    for key in ("tmdb_api", "base_url", "upstream_repo", "upstream_branch",
                "admin_username", "admin_password", "session_secret", "http_proxy_url",
                "mediaflow_password", "payment_instructions", "payment_qr_url",
                "announcement_channel", "skip_channel"):
        if key in payload and isinstance(payload[key], str):
            payload[key] = payload[key].strip()

    if payload.get("admin_password"):
        payload["admin_password"] = hash_password(payload["admin_password"])

    try:
        reinit_results = await SettingsManager.update(db, payload)
        return {
            "message": "Settings saved successfully.",
            "reinit": reinit_results,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
