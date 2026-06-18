"""
Usage
-----

    # Anywhere in the app:
    s = SettingsManager.current()
    if s.replace_mode:
        ...
    proxy = s.http_proxy_url

    # In admin API (save + reinit changed subsystems):
    reinit_results = await SettingsManager.update(db, {"replace_mode": False})
"""
from __future__ import annotations

from typing import Any, Dict, List

from Backend.logger import LOGGER


# ─────────────────────────────────────────────────────────────────────────────
# Default values (used when nothing exists in the DB yet)
# ─────────────────────────────────────────────────────────────────────────────
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
    "subscription": False,
    "subscription_group_id": 0,
    "subscription_url": "https://t.me/",
    "approver_ids": [],
    "http_proxy_url": "",
    "show_proxy_and_non_proxy_both": False,
    "multi_tokens": [],
}


def _seed_from_env() -> Dict[str, Any]:
    """
    Read legacy Telegram config env values and return them as a settings dict.
    Called only on FIRST startup to migrate existing config.env values into DB.
    """
    from Backend.config import Telegram  # lazy import to avoid circular deps

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
        "admin_password":               Telegram.ADMIN_PASSWORD,
        "subscription":                 Telegram.SUBSCRIPTION,
        "subscription_group_id":        Telegram.SUBSCRIPTION_GROUP_ID,
        "subscription_url":             Telegram.SUBSCRIPTION_URL,
        "approver_ids":                 list(Telegram.APPROVER_IDS),
        "http_proxy_url":               Telegram.HTTP_PROXY_URL,
        "show_proxy_and_non_proxy_both": Telegram.SHOW_PROXY_AND_NON_PROXY_BOTH,
        "multi_tokens":                 [],
    })
    return seed


# ─────────────────────────────────────────────────────────────────────────────
# Immutable settings snapshot
# ─────────────────────────────────────────────────────────────────────────────
class Settings:
    """
    Immutable value object returned by SettingsManager.current().

    All attributes are typed; lists are always returned as copies so callers
    cannot accidentally mutate the stored state.
    """

    __slots__ = ("_d",)

    def __init__(self, data: Dict[str, Any]) -> None:
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in data.items() if k != "_id"})
        self._d = merged

    # ── Booleans ─────────────────────────────────────────────────────────────
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

    # ── Strings ──────────────────────────────────────────────────────────────
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
    def http_proxy_url(self) -> str:
        return str(self._d.get("http_proxy_url") or "")

    @property
    def subscription_url(self) -> str:
        return str(self._d.get("subscription_url") or "https://t.me/")

    # ── Integers ─────────────────────────────────────────────────────────────
    @property
    def subscription_group_id(self) -> int:
        return int(self._d.get("subscription_group_id") or 0)

    # ── Lists ─────────────────────────────────────────────────────────────────
    @property
    def auth_channels(self) -> List[str]:
        return list(self._d.get("auth_channels") or [])

    @property
    def approver_ids(self) -> List[int]:
        return [int(x) for x in (self._d.get("approver_ids") or [])]

    @property
    def multi_tokens(self) -> List[str]:
        return list(self._d.get("multi_tokens") or [])

    # ── Serialisation ─────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return dict(self._d)


# ─────────────────────────────────────────────────────────────────────────────
# Manager singleton
# ─────────────────────────────────────────────────────────────────────────────
class SettingsManager:
    """
    Singleton that owns the authoritative in-memory settings snapshot.

    Thread/coroutine safety note: all mutations go through `update()`, which
    awaits a DB write before swapping `_current`. There is a brief window where
    a concurrent reader could see stale data; for an admin panel that is
    acceptable. Add an asyncio.Lock here if stricter consistency is needed.
    """

    _current: Settings | None = None

    # ── Bootstrap ────────────────────────────────────────────────────────────
    @classmethod
    async def initialize(cls, db) -> None:
        """
        Call once after ``db.connect()``.

        - First run  → seeds the DB from legacy env values then loads.
        - Later runs → loads existing DB values, merges with defaults for
          any new keys added in a future release.
        """
        raw = await db.get_settings()
        if not raw:
            LOGGER.info("SettingsManager: no settings in DB — seeding from config.env.")
            seed = _seed_from_env()
            await db.save_settings(seed)
            cls._current = Settings(seed)
        else:
            cls._current = Settings(raw)
        LOGGER.info("SettingsManager: settings loaded successfully.")

    @classmethod
    async def reload(cls, db) -> None:
        """Reload settings from DB (call after an external change)."""
        raw = await db.get_settings()
        if raw:
            cls._current = Settings(raw)

    # ── Read ─────────────────────────────────────────────────────────────────
    @classmethod
    def current(cls) -> Settings:
        """Return the current settings snapshot. Always safe to call."""
        if cls._current is None:
            return Settings({})   # safe fallback before initialize()
        return cls._current

    # ── Write + reinitialise ─────────────────────────────────────────────────
    @classmethod
    async def update(cls, db, new_values: Dict[str, Any]) -> Dict[str, str]:
        """
        Merge *new_values* onto existing settings, persist to DB, reload
        the in-memory snapshot, and reinitialise changed subsystems.

        Returns a ``{subsystem: status_message}`` dict for the API response.
        """
        old = cls.current().to_dict()
        merged = dict(old)
        merged.update(new_values)

        await db.save_settings(merged)
        await cls.reload(db)
        new = cls.current().to_dict()

        return await cls._reinit_changed(old, new)

    # ── Internal reinit logic ────────────────────────────────────────────────
    @classmethod
    async def _reinit_changed(cls, old: dict, new: dict) -> Dict[str, str]:
        results: Dict[str, str] = {}

        # Multi-tokens changed → reinitialise Pyrogram helper clients
        old_tokens = set(old.get("multi_tokens") or [])
        new_tokens = set(new.get("multi_tokens") or [])
        if old_tokens != new_tokens:
            try:
                from Backend.pyrofork.clients import initialize_clients
                await initialize_clients()
                results["multi_tokens"] = "clients reinitialized"
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit multi_tokens: {exc}")
                results["multi_tokens"] = f"error: {exc}"

        # Auth channels changed → reload channel cache
        if (old.get("auth_channels") or []) != (new.get("auth_channels") or []):
            try:
                from Backend.pyrofork.plugins.channels import _load_channels_from_db
                await _load_channels_from_db()
                results["auth_channels"] = "channel cache reloaded"
            except Exception as exc:
                LOGGER.error(f"SettingsManager reinit auth_channels: {exc}")
                results["auth_channels"] = f"error: {exc}"

        # Proxy settings changed
        proxy_keys = {"http_proxy_url", "show_proxy_and_non_proxy_both"}
        if any(old.get(k) != new.get(k) for k in proxy_keys):
            results["proxy"] = "updated — applies to next outbound request"

        # Subscription settings changed
        sub_keys = {"subscription", "subscription_group_id", "approver_ids", "subscription_url"}
        if any(old.get(k) != new.get(k) for k in sub_keys):
            results["subscription"] = "settings reloaded in-memory"

        # Admin credentials changed
        cred_keys = {"admin_username", "admin_password"}
        if any(old.get(k) != new.get(k) for k in cred_keys):
            results["admin_credentials"] = "updated — takes effect on next login"

        return results
