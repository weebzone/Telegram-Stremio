from __future__ import annotations

import re
from typing import Any, Dict, List

from Backend.config import Telegram

_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

_LIST_LABELS = {
    "auth_channels": "Auth channels",
    "multi_tokens": "Bot tokens",
    "extra_databases": "Databases",
    "global_search_channels": "Global search channels",
    "anime_channels": "Anime channels",
    "manual_channels": "Manual channels",
    "approver_ids": "Approver IDs",
}


def _is_id(value: str) -> bool:
    value = value.strip()
    return bool(value) and value.lstrip("-").isdigit()


def _list(p: Dict[str, Any], key: str) -> list:
    v = p.get(key)
    return v if isinstance(v, list) else []


#----- config.env checks (run at startup); returns problems in plain language
def validate_env() -> List[str]:
    e: List[str] = []
    if Telegram.API_ID <= 0:
        e.append("API_ID is missing or not a number (get it from my.telegram.org).")
    if not Telegram.API_HASH:
        e.append("API_HASH is missing (get it from my.telegram.org).")
    if not _BOT_TOKEN_RE.match(Telegram.BOT_TOKEN or ""):
        e.append("BOT_TOKEN is missing or malformed (copy it exactly from @BotFather).")
    if not Telegram.DATABASE:
        e.append("DATABASE is empty (add at least one MongoDB link).")
    else:
        for uri in Telegram.DATABASE:
            if not uri.startswith(("mongodb://", "mongodb+srv://")):
                e.append(f"DATABASE link must start with mongodb:// or mongodb+srv:// (got: {uri[:24]}…).")
    if Telegram.OWNER_ID <= 0:
        e.append("OWNER_ID is missing or not a number (set it to your Telegram user id).")
    if not (0 < Telegram.PORT <= 65535):
        e.append("PORT must be a number between 1 and 65535.")
    return e


#----- Settings-page checks; payload is already type-coerced. Returns problems in plain language
def validate_settings(p: Dict[str, Any]) -> List[str]:
    e: List[str] = []

    for key, label in _LIST_LABELS.items():
        if key in p and not isinstance(p[key], list):
            e.append(f"{label} must be a list of values.")

    url = p.get("base_url")
    if url and not str(url).startswith(("http://", "https://")):
        e.append("Base URL must start with http:// or https://.")

    repo = p.get("upstream_repo")
    if repo and not str(repo).startswith(("http://", "https://")):
        e.append("Upstream repo must be a link starting with http:// or https://.")

    proxy = p.get("http_proxy_url")
    if proxy and not str(proxy).startswith(("http://", "https://", "socks5://", "socks4://")):
        e.append("Proxy URL must start with http://, https:// or socks5://.")

    if "admin_username" in p and not str(p["admin_username"]).strip():
        e.append("Admin username can't be empty.")

    if p.get("admin_password") and len(str(p["admin_password"])) < 4:
        e.append("Admin password is too short (use at least 4 characters).")

    for uri in _list(p, "extra_databases"):
        if not str(uri).startswith(("mongodb://", "mongodb+srv://")):
            e.append(f"Database link must start with mongodb:// or mongodb+srv:// (got: {str(uri)[:24]}…).")

    for tok in _list(p, "multi_tokens"):
        if not _BOT_TOKEN_RE.match(str(tok)):
            e.append(f"Bot token looks wrong — expected like 123456:AA… (got: {str(tok)[:12]}…).")

    for key, label in (("global_search_channels", "Global search"),
                       ("anime_channels", "Anime"),
                       ("manual_channels", "Manual")):
        for ch in _list(p, key):
            if not _is_id(str(ch).replace("-100", "")):
                e.append(f"{label} channel id must be a number (got: {ch}).")

    for v in _list(p, "approver_ids"):
        if not _is_id(str(v)):
            e.append(f"Approver ID must be a number (got: {v}).")
            break

    gid = p.get("subscription_group_id")
    if gid not in (None, "") and not _is_id(str(gid)):
        e.append("Subscription group ID must be a number.")

    ann = p.get("announcement_channel")
    if ann and not (str(ann).startswith("@") or _is_id(str(ann))):
        e.append("Announcement channel must be a number or start with @.")

    return e
