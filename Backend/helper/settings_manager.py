from __future__ import annotations

import secrets
from typing import Any, Dict, List

import Backend.pyrofork.bot as botmod
from Backend.config import Telegram
from Backend.helper.passwords import hash_password
from Backend.logger import LOGGER

#----- Default values (used when nothing exists in the DB yet)
_DEFAULTS: Dict[str, Any] = {
    "replace_mode": True,
    "duplicate_protection": False,
    "hide_catalog": False,
    "auth_channels": [],
    "tmdb_api": "",
    "tvdb_api": "",
    "base_url": "",
    "upstream_repo": "",
    "upstream_branch": "master",
    "admin_username": "admin",
    "admin_password": "admin",
    "session_secret": "",
    "http_proxy_url": "",
    "cf_stream_url": "",
    "cf_stream_secret": "",
    "cf_stream_mode": "off",
    "show_proxy_and_non_proxy_both": False,
    "mediaflow_proxy": False,
    "mediaflow_password": "",
    "multi_tokens": [],
    "extra_databases": [],
    "anime_channels": [],
    "manual_channels": [],
    "channel_titles": {},
    "announce_new_content": False,
    "announcement_channel": "",
    "skip_channel": "",
    "delete_on_metadata_fail": False,
    "better_poster_enabled": False,
    "better_poster": "",
    "rpdb_enabled": False,
    "rpdb_api_key": "",
    "spatial_poster_enabled": False,
    "spatial_poster": "",
    "stream_formatter": "default",
    "formatter_name": "",
    "formatter_description": "",
    "fanart_enabled": False,
    "fanart_api_key": "",
    "fanart_shuffle": False,
    "fanart_shuffle_interval": 5,
    "fanart_low_res_poster": True,
}


#----- Read legacy Telegram config env values; used only on FIRST startup
def _seed_from_env() -> Dict[str, Any]:
    seed = dict(_DEFAULTS)
    seed.update({
        "replace_mode":                 Telegram.REPLACE_MODE,
        "hide_catalog":                 Telegram.HIDE_CATALOG,
        "auth_channels":                list(Telegram.AUTH_CHANNEL),
        "tmdb_api":                     Telegram.TMDB_API,
        "tvdb_api":                     getattr(Telegram, "TVDB_API", "") or "",
        "base_url":                     Telegram.BASE_URL,
        "upstream_repo":                Telegram.UPSTREAM_REPO,
        "upstream_branch":              Telegram.UPSTREAM_BRANCH,
        "admin_username":               Telegram.ADMIN_USERNAME,
        "admin_password":               hash_password(Telegram.ADMIN_PASSWORD),
        "session_secret":               secrets.token_hex(32),
        "http_proxy_url":               Telegram.HTTP_PROXY_URL,
        "show_proxy_and_non_proxy_both": Telegram.SHOW_PROXY_AND_NON_PROXY_BOTH,
        "multi_tokens":                 [],
        "extra_databases":              list(Telegram.DATABASE[2:]) if len(Telegram.DATABASE) > 2 else [],
    })
    return seed


#----- Immutable settings snapshot
class Settings:
    __slots__ = ("_d",)

    def __init__(self, data: Dict[str, Any]) -> None:
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in data.items() if k != "_id"})
        self._d = merged

    #----- Booleans
    @property
    def replace_mode(self) -> bool:
        return bool(self._d["replace_mode"])

    @property
    def duplicate_protection(self) -> bool:
        return bool(self._d.get("duplicate_protection", False))

    @property
    def hide_catalog(self) -> bool:
        return bool(self._d["hide_catalog"])

    @property
    def show_proxy_and_non_proxy_both(self) -> bool:
        return bool(self._d["show_proxy_and_non_proxy_both"])

    @property
    def mediaflow_proxy(self) -> bool:
        return bool(self._d.get("mediaflow_proxy", False))

    @property
    def anime_channels(self) -> List[str]:
        return list(self._d.get("anime_channels") or [])

    @property
    def manual_channels(self) -> List[str]:
        return list(self._d.get("manual_channels") or [])

    @property
    def channel_titles(self) -> Dict[str, str]:
        raw = self._d.get("channel_titles") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if k and v}

    @property
    def announce_new_content(self) -> bool:
        return bool(self._d.get("announce_new_content", False))

    @property
    def delete_on_metadata_fail(self) -> bool:
        return bool(self._d.get("delete_on_metadata_fail", False))

    @property
    def announcement_channel(self) -> str:
        return str(self._d.get("announcement_channel") or "").strip()

    @property
    def skip_channel(self) -> str:
        return str(self._d.get("skip_channel") or "").strip()

    #----- Strings
    @property
    def tmdb_api(self) -> str:
        return str(self._d.get("tmdb_api") or "")

    @property
    def tvdb_api(self) -> str:
        return str(self._d.get("tvdb_api") or "").strip()

    @property
    def base_url(self) -> str:
        return str(self._d.get("base_url") or "").rstrip("/")

    @property
    def upstream_repo(self) -> str:
        return str(self._d.get("upstream_repo") or "")

    @property
    def upstream_branch(self) -> str:
        return str(self._d.get("upstream_branch") or "")

    @property
    def admin_username(self) -> str:
        return str(self._d.get("admin_username") or "admin")

    @property
    def admin_password(self) -> str:
        return str(self._d.get("admin_password") or "admin")

    @property
    def session_secret(self) -> str:
        return str(self._d.get("session_secret") or "")

    @property
    def http_proxy_url(self) -> str:
        return str(self._d.get("http_proxy_url") or "")

    @property
    def cf_stream_url(self) -> str:
        return str(self._d.get("cf_stream_url") or "").rstrip("/")

    @property
    def cf_stream_secret(self) -> str:
        return str(self._d.get("cf_stream_secret") or "")

    #----- "off", "cloudflare" (CF links only) or "both" (CF + direct links)
    @property
    def cf_stream_mode(self) -> str:
        mode = str(self._d.get("cf_stream_mode") or "off")
        return mode if mode in ("off", "cloudflare", "both") else "off"

    @property
    def mediaflow_password(self) -> str:
        return str(self._d.get("mediaflow_password") or "")

    @property
    def better_poster_enabled(self) -> bool:
        return bool(self._d.get("better_poster_enabled", False))

    @property
    def better_poster(self) -> str:
        return str(self._d.get("better_poster") or "").strip()

    @property
    def rpdb_enabled(self) -> bool:
        return bool(self._d.get("rpdb_enabled", False))

    @property
    def rpdb_api_key(self) -> str:
        return str(self._d.get("rpdb_api_key") or "").strip()

    @property
    def stream_formatter(self) -> str:
        return str(self._d.get("stream_formatter") or "default")

    @property
    def formatter_name(self) -> str:
        return str(self._d.get("formatter_name") or "")

    @property
    def formatter_description(self) -> str:
        return str(self._d.get("formatter_description") or "")

    @property
    def spatial_poster_enabled(self) -> bool:
        return bool(self._d.get("spatial_poster_enabled", False))

    @property
    def spatial_poster(self) -> str:
        return str(self._d.get("spatial_poster") or "").strip()

    @property
    def fanart_enabled(self) -> bool:
        return bool(self._d.get("fanart_enabled", False))

    @property
    def fanart_api_key(self) -> str:
        return str(self._d.get("fanart_api_key") or "").strip()

    @property
    def fanart_shuffle(self) -> bool:
        return bool(self._d.get("fanart_shuffle", False))

    @property
    def fanart_low_res_poster(self) -> bool:
        return bool(self._d.get("fanart_low_res_poster", True))

    #----- Integers
    @property
    def fanart_shuffle_interval(self) -> int:
        try:
            return max(0, int(self._d.get("fanart_shuffle_interval", 5)))
        except (ValueError, TypeError):
            return 5

    #----- Lists
    @property
    def auth_channels(self) -> List[str]:
        return list(self._d.get("auth_channels") or [])

    @property
    def multi_tokens(self) -> List[str]:
        return list(self._d.get("multi_tokens") or [])

    @property
    def extra_databases(self) -> List[str]:
        return list(self._d.get("extra_databases") or [])

    #----- Serialisation
    def to_dict(self) -> Dict[str, Any]:
        return dict(self._d)


#----- Manager singleton exposing the current settings snapshot
class SettingsManager:
    _current: Settings | None = None

    #----- Bootstrap from DB, seeding from env on first run
    @classmethod
    async def initialize(cls, db) -> None:
        raw = await db.get_settings()
        if not raw:
            LOGGER.info("SettingsManager: no settings in DB — seeding from config.env.")
            seed = _seed_from_env()
            await db.save_settings(seed)
            cls._current = Settings(seed)
        else:
            cls._current = Settings(raw)

        #----- Backfill & persist a session secret for installs that predate this setting,
        #----- otherwise a new random key would be generated every restart (logging admins out)
        if not cls._current.session_secret:
            data = cls._current.to_dict()
            data["session_secret"] = secrets.token_hex(32)
            await db.save_settings(data)
            cls._current = Settings(data)
            LOGGER.info("SettingsManager: generated and stored a new persistent session secret.")

        LOGGER.info("SettingsManager: settings loaded successfully.")

    #----- Reload settings from DB (call after an external change)
    @classmethod
    async def reload(cls, db) -> None:
        raw = await db.get_settings()
        if raw:
            cls._current = Settings(raw)

    #----- Current snapshot (empty defaults if uninitialised)
    @classmethod
    def current(cls) -> Settings:
        if cls._current is None:
            return Settings({})
        return cls._current

    @staticmethod
    def _all_channel_ids(data: dict) -> set:
        ids = set()
        for key in ("auth_channels", "manual_channels", "anime_channels"):
            for c in (data.get(key) or []):
                c = str(c).strip()
                if c:
                    ids.add(c)
        for key in ("announcement_channel", "skip_channel"):
            v = str(data.get(key) or "").strip()
            if v:
                ids.add(v)
        return ids

    @classmethod
    async def _sync_channel_titles(cls, data: dict) -> dict:
        active = cls._all_channel_ids(data)
        existing = data.get("channel_titles") or {}
        if not isinstance(existing, dict):
            existing = {}
        titles = {str(k): str(v) for k, v in existing.items() if str(k) in active and v}
        missing = [cid for cid in active if cid not in titles]
        if missing:
            clients = []
            stream = getattr(botmod, "StreamBot", None)
            if stream is not None:
                clients.append(stream)
            for cid in missing:
                resolved = None
                for client in clients:
                    try:
                        chat = await client.get_chat(int(cid))
                        if chat and getattr(chat, "title", None):
                            resolved = chat.title
                            break
                    except Exception:
                        continue
                if resolved:
                    titles[cid] = resolved
        data["channel_titles"] = titles
        return titles

    #----- Persist new values, flip the snapshot, and reinitialise dependents
    @classmethod
    async def update(cls, db, new_values: Dict[str, Any]) -> Dict[str, str]:
        old = cls.current().to_dict()
        merged = dict(old)
        merged.update(new_values)

        results: Dict[str, str] = {}

        await cls._sync_channel_titles(merged)

        #----- Phase 1: validate/apply changes that can abort the save
        old_extra = old.get("extra_databases") or []
        new_extra = merged.get("extra_databases") or []
        if old_extra != new_extra:
            result = await db.reload_extra_databases(new_extra)
            results["databases"] = result.get("message", "databases reloaded")

        #----- Phase 2: persist and flip the in-memory snapshot
        await db.save_settings(merged)
        cls._current = Settings(merged)

        #----- Phase 3: reinit everything that reads current()
        results.update(await cls._reinit_dependent(old, merged))

        return results

    #----- Reinit logic (runs AFTER _current has been updated)
    @classmethod
    async def _reinit_dependent(cls, old: dict, new: dict) -> Dict[str, str]:
        results: Dict[str, str] = {}

        #----- Multi-tokens changed: hot-reload Pyrogram helper clients
        old_tokens = old.get("multi_tokens") or []
        new_tokens = new.get("multi_tokens") or []
        if old_tokens != new_tokens:
            try:
                from Backend.pyrofork.clients import reload_multi_token_clients
                result = await reload_multi_token_clients()
                results["multi_tokens"] = (
                    f"{result['started']} started, {result['stopped']} stopped "
                    f"({result['total_clients']} active)"
                )
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit multi_tokens: {exc}")
                results["multi_tokens"] = f"error: {exc}"

        #----- Auth channels changed
        old_channels = old.get("auth_channels") or []
        new_channels = new.get("auth_channels") or []
        if old_channels != new_channels:
            results["auth_channels"] = f"{len(new_channels)} channel(s) saved"

        #----- Proxy settings changed
        proxy_keys = {"http_proxy_url", "show_proxy_and_non_proxy_both", "mediaflow_proxy", "mediaflow_password"}
        if any(old.get(k) != new.get(k) for k in proxy_keys):
            results["proxy"] = "updated — applies to next outbound request"

        #----- Cloudflare streaming settings changed (read live per request)
        cf_keys = {"cf_stream_url", "cf_stream_secret", "cf_stream_mode"}
        if any(old.get(k) != new.get(k) for k in cf_keys):
            results["cloudflare"] = "updated — new stream links use it right away"

        #----- Bot tokens or Cloudflare settings changed: have the Worker reload them now
        if old_tokens != new_tokens or any(old.get(k) != new.get(k) for k in cf_keys):
            from Backend.helper.cf_stream import sync_worker_soon
            sync_worker_soon()

        #----- Admin credentials changed
        cred_keys = {"admin_username", "admin_password"}
        if any(old.get(k) != new.get(k) for k in cred_keys):
            results["admin_credentials"] = "updated — takes effect on next login"

        return results
