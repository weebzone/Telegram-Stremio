from __future__ import annotations

import secrets
from typing import Any, Dict, List

from Backend.config import Telegram
from Backend.helper.passwords import hash_password
from Backend.logger import LOGGER

#----- Default values (used when nothing exists in the DB yet)
_DEFAULTS: Dict[str, Any] = {
    "replace_mode": True,
    "hide_catalog": False,
    "auth_channels": [],
    "tmdb_api": "",
    "base_url": "",
    "upstream_repo": "",
    "upstream_branch": "",
    "admin_username": "admin",
    "admin_password": "admin",
    "session_secret": "",
    "subscription": False,
    "subscription_group_id": 0,
    "approver_ids": [],
    "payment_instructions": "",
    "payment_qr_url": "",
    "http_proxy_url": "",
    "show_proxy_and_non_proxy_both": False,
    "multi_tokens": [],
    "extra_databases": [],
    "global_search": False,
    "global_search_channels": [],
    "anime_channels": [],
}


#----- Read legacy Telegram config env values; used only on FIRST startup
def _seed_from_env() -> Dict[str, Any]:
    seed = dict(_DEFAULTS)
    seed.update({
        "replace_mode":                 Telegram.REPLACE_MODE,
        "hide_catalog":                 Telegram.HIDE_CATALOG,
        "auth_channels":                list(Telegram.AUTH_CHANNEL),
        "tmdb_api":                     Telegram.TMDB_API,
        "base_url":                     Telegram.BASE_URL,
        "upstream_repo":                Telegram.UPSTREAM_REPO,
        "upstream_branch":              Telegram.UPSTREAM_BRANCH,
        "admin_username":               Telegram.ADMIN_USERNAME,
        "admin_password":               hash_password(Telegram.ADMIN_PASSWORD),
        "session_secret":               secrets.token_hex(32),
        "subscription":                 Telegram.SUBSCRIPTION,
        "subscription_group_id":        Telegram.SUBSCRIPTION_GROUP_ID,
        "approver_ids":                 list(Telegram.APPROVER_IDS),
        "global_search_channels":       [],
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
    def hide_catalog(self) -> bool:
        return bool(self._d["hide_catalog"])

    @property
    def subscription(self) -> bool:
        return bool(self._d["subscription"])

    @property
    def show_proxy_and_non_proxy_both(self) -> bool:
        return bool(self._d["show_proxy_and_non_proxy_both"])

    @property
    def global_search(self) -> bool:
        return bool(self._d.get("global_search", False))

    @property
    def global_search_channels(self):
        return list(self._d.get("global_search_channels") or [])

    @property
    def anime_channels(self) -> List[str]:
        return list(self._d.get("anime_channels") or [])

    #----- Strings
    @property
    def tmdb_api(self) -> str:
        return str(self._d.get("tmdb_api") or "")

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
    def payment_instructions(self) -> str:
        return str(self._d.get("payment_instructions") or "")

    @property
    def payment_qr_url(self) -> str:
        return str(self._d.get("payment_qr_url") or "")

    #----- Integers
    @property
    def subscription_group_id(self) -> int:
        return int(self._d.get("subscription_group_id") or 0)

    #----- Lists
    @property
    def auth_channels(self) -> List[str]:
        return list(self._d.get("auth_channels") or [])

    @property
    def approver_ids(self) -> List[int]:
        return [int(x) for x in (self._d.get("approver_ids") or [])]

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

    #----- Persist new values, flip the snapshot, and reinitialise dependents
    @classmethod
    async def update(cls, db, new_values: Dict[str, Any]) -> Dict[str, str]:
        old = cls.current().to_dict()
        merged = dict(old)
        merged.update(new_values)

        results: Dict[str, str] = {}

        #----- Global Search requires a Userbot session; enforce it server-side
        if merged.get("global_search"):
            if not Telegram.USER_SESSION_STRING:
                merged["global_search"] = False
                LOGGER.warning(
                    "SettingsManager: rejected global_search=True — "
                    "USER_SESSION_STRING is not configured."
                )
                results["global_search"] = "rejected — no Userbot session configured"

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
        proxy_keys = {"http_proxy_url", "show_proxy_and_non_proxy_both"}
        if any(old.get(k) != new.get(k) for k in proxy_keys):
            results["proxy"] = "updated — applies to next outbound request"

        #----- Subscription enabled/disabled: start or stop the checker task
        if old.get("subscription") != new.get("subscription"):
            try:
                from Backend.helper import subscription_task_manager
                from Backend.pyrofork.bot import StreamBot

                if new.get("subscription"):
                    await subscription_task_manager.start(StreamBot)
                    results["subscription"] = "checker task started"
                else:
                    await subscription_task_manager.stop()
                    results["subscription"] = "checker task stopped"
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit subscription: {exc}")
                results["subscription"] = f"error: {exc}"
        else:
            sub_keys = {"subscription_group_id", "approver_ids",
                        "payment_instructions", "payment_qr_url"}
            if any(old.get(k) != new.get(k) for k in sub_keys):
                results["subscription"] = "settings reloaded in-memory"

        #----- Admin credentials changed
        cred_keys = {"admin_username", "admin_password"}
        if any(old.get(k) != new.get(k) for k in cred_keys):
            results["admin_credentials"] = "updated — takes effect on next login"

        #----- Global Search toggle changed (module reads current() live per call)
        if old.get("global_search") != new.get("global_search") and "global_search" not in results:
            results["global_search"] = "enabled" if new.get("global_search") else "disabled"

        return results
