from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any, Dict, List, Tuple

from Backend.config import Telegram

_BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
_ENCODE_CHARS = set(" /?#[]")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")

#----- Marker used to confirm a Base URL actually points to THIS running instance
INSTANCE_ID = secrets.token_hex(8)

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


#----- A channel id is a -100… id or an @username (plain numbers allowed only where noted)
def _channel_ok(ch: str, allow_plain: bool = False) -> bool:
    ch = str(ch).strip()
    if ch.startswith("@"):
        return len(ch) > 1
    if ch.startswith("-100") and ch[1:].isdigit():
        return True
    return allow_plain and ch.lstrip("-").isdigit()


def _to_chat_id(ch: str):
    ch = str(ch).strip()
    return int(ch) if ch.lstrip("-").isdigit() else ch


#----- Split a mongo URI into (userinfo, lowercased host) ignoring db name and query
def _mongo_authority(uri: str) -> Tuple[str, str]:
    rest = uri.split("://", 1)[1]
    authority = rest.split("/", 1)[0].split("?", 1)[0]
    if "@" in authority:
        userinfo, host = authority.rsplit("@", 1)
    else:
        userinfo, host = "", authority
    return userinfo, host.lower()


#----- Deep MongoDB checks: format, credentials, encoding and duplicate clusters
def _check_mongo(uris: List[str], label: str, seen: Dict[Tuple[str, str], str]) -> List[str]:
    e: List[str] = []
    for i, raw in enumerate(uris, 1):
        uri = str(raw).strip()
        tag = f"{label} #{i}"
        if not uri:
            continue
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            e.append(f"{tag} must start with mongodb:// or mongodb+srv:// (got: {uri[:24]}…).")
            continue
        if uri.count("mongodb://") + uri.count("mongodb+srv://") > 1:
            e.append(f"{tag} looks like two links stuck together — separate each MongoDB with a comma.")
            continue

        userinfo, host = _mongo_authority(uri)
        is_local = any(h in host for h in _LOCAL_HOSTS)
        user = ""
        if not userinfo:
            if not is_local:
                e.append(f"{tag} has no username/password — a MongoDB Atlas link needs user:pass@host.")
                continue
        else:
            if "@" in userinfo:
                e.append(f"{tag} password has an unescaped '@' — write it as %40.")
                continue
            user, sep, pwd = userinfo.partition(":")
            if not user.strip():
                e.append(f"{tag} is missing the username.")
                continue
            if not sep or not pwd:
                e.append(f"{tag} is missing the password (a common mistake).")
                continue
            bad = sorted(_ENCODE_CHARS & set(pwd))
            if bad:
                shown = " ".join("space" if c == " " else c for c in bad)
                e.append(f"{tag} password has characters that must be URL-encoded: {shown}.")
                continue

        key = (user.strip().lower(), host)
        if host and key in seen:
            e.append(f"{tag} points to the same MongoDB as {seen[key]} — use a different cluster, not just a different database name.")
        elif host:
            seen[key] = tag
    return e


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
        if len(Telegram.DATABASE) < 2:
            e.append("DATABASE needs at least 2 links (1 tracking + 1 storage), separated by a comma.")
        e += _check_mongo(Telegram.DATABASE, "DATABASE", {})

    if Telegram.OWNER_ID <= 0:
        e.append("OWNER_ID is missing or not a number (set it to your Telegram user id).")
    if not (0 < Telegram.PORT <= 65535):
        e.append("PORT must be a number between 1 and 65535.")
    return e


#----- Settings-page format checks (offline). Payload is already type-coerced.
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

    #----- Extra databases: deep checks + must differ from the config.env ones
    seen: Dict[Tuple[str, str], str] = {}
    for uri in Telegram.DATABASE:
        u = str(uri).strip()
        if u.startswith(("mongodb://", "mongodb+srv://")):
            info, host = _mongo_authority(u)
            if host:
                seen[(info.partition(":")[0].strip().lower(), host)] = "a config.env database"
    e += _check_mongo(_list(p, "extra_databases"), "Database", seen)

    for tok in _list(p, "multi_tokens"):
        if not _BOT_TOKEN_RE.match(str(tok)):
            e.append(f"Bot token looks wrong — expected like 123456:AA… (got: {str(tok)[:12]}…).")

    for key, label in (("auth_channels", "Auth"), ("anime_channels", "Anime"), ("manual_channels", "Manual")):
        for ch in _list(p, key):
            if not _channel_ok(ch):
                e.append(f"{label} channel '{ch}' looks wrong — use the -100… id or an @username.")
    for ch in _list(p, "global_search_channels"):
        if not _channel_ok(ch, allow_plain=True):
            e.append(f"Global search channel '{ch}' looks wrong — use the -100… id or an @username.")

    for v in _list(p, "approver_ids"):
        if not _is_id(str(v)):
            e.append(f"Approver ID must be a number (got: {v}).")
            break

    gid = p.get("subscription_group_id")
    if gid not in (None, "") and not _is_id(str(gid)):
        e.append("Subscription group ID must be a number.")

    ann = p.get("announcement_channel")
    if ann and not _channel_ok(ann):
        e.append("Announcement channel must be a -100… id or an @username.")

    return e


#----- Live checks (network): base URL identity, DB connectivity, bot admin rights
async def validate_settings_live(p: Dict[str, Any]) -> List[str]:
    e: List[str] = []
    e += await _check_base_url(p)
    e += await _check_extra_dbs_live(p)
    e += await _check_channels_live(p)
    return e


async def _check_base_url(p: Dict[str, Any]) -> List[str]:
    if "base_url" not in p:
        return []
    from Backend.helper.settings_manager import SettingsManager
    url = str(p.get("base_url") or "").strip().rstrip("/")
    if url == str(SettingsManager.current().base_url or "").strip().rstrip("/"):
        return []
    if not url or not url.startswith(("http://", "https://")):
        return []
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
            r = await c.get(f"{url}/api/instance")
        if r.status_code != 200 or r.json().get("instance") != INSTANCE_ID:
            return [f"Base URL '{url}' does not point to this server — double-check the address."]
    except Exception:
        return [f"Base URL '{url}' isn't reachable from here — make sure it's this server's public address."]
    return []


async def _check_extra_dbs_live(p: Dict[str, Any]) -> List[str]:
    if "extra_databases" not in p:
        return []
    from Backend.helper.settings_manager import SettingsManager
    current = [str(u).strip() for u in SettingsManager.current().extra_databases]
    uris = [u for u in (str(u).strip() for u in _list(p, "extra_databases")) if u and u not in current]
    e: List[str] = []
    import motor.motor_asyncio as motor
    for i, uri in enumerate(uris, 1):
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            continue
        client = None
        try:
            client = motor.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=6000)
            await client.admin.command("ping")
        except Exception as ex:
            e.append(f"Database #{i} won't connect ({type(ex).__name__}) — check the link, password and IP allowlist.")
        finally:
            if client:
                client.close()
    return e


#----- Confirm a bot is an admin that can post in a chat; returns (ok, plain reason)
async def _admin_write(client, chat_id) -> Tuple[bool, str]:
    from pyrogram.enums import ChatMemberStatus
    try:
        member = await asyncio.wait_for(client.get_chat_member(chat_id, "me"), timeout=15)
    except asyncio.TimeoutError:
        return False, "couldn't be checked (timed out)"
    except Exception as ex:
        name = type(ex).__name__
        if name == "UserNotParticipant":
            return False, "hasn't been added to this channel"
        if name in ("PeerIdInvalid", "ChannelInvalid", "ChannelPrivate"):
            return False, "can't see this channel (wrong id or not joined)"
        return False, f"couldn't be checked ({name})"
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return False, "isn't an admin here"
    priv = getattr(member, "privileges", None)
    if priv is not None and not getattr(priv, "can_post_messages", True):
        return False, "is an admin but can't post messages"
    return True, ""


def _bot_name(client, i: int) -> str:
    u = getattr(getattr(client, "me", None), "username", None)
    return f"@{u}" if u else f"Bot {i}"


#----- Main bot + every multi-token bot; brand-new tokens are temporarily started
async def _resolve_bots(p: Dict[str, Any]):
    from pyrogram import Client
    from Backend.helper.settings_manager import SettingsManager
    from Backend.pyrofork.bot import StreamBot, multi_clients
    from Backend.pyrofork.clients import client_tokens

    bots: List[Tuple[str, Any]] = [("Main bot", StreamBot)]
    temps: List[Any] = []

    tokens = p.get("multi_tokens")
    if not isinstance(tokens, list):
        tokens = SettingsManager.current().multi_tokens
    by_token = {tok: cid for cid, tok in client_tokens.items()}

    for i, tok in enumerate([str(t).strip() for t in tokens if str(t).strip()], 1):
        cid = by_token.get(tok)
        if cid is not None and cid in multi_clients:
            bots.append((_bot_name(multi_clients[cid], i), multi_clients[cid]))
            continue
        try:
            client = await Client(
                name=f"chk_{i}_{secrets.token_hex(3)}",
                api_id=Telegram.API_ID, api_hash=Telegram.API_HASH,
                bot_token=tok, no_updates=True, in_memory=True,
            ).start()
            temps.append(client)
            bots.append((_bot_name(client, i), client))
        except Exception:
            bots.append((f"Bot {i}", None))
    return bots, temps


async def _check_channels_live(p: Dict[str, Any]) -> List[str]:
    from Backend.helper.settings_manager import SettingsManager

    relevant = {"auth_channels", "manual_channels", "anime_channels", "multi_tokens", "announcement_channel"}
    if not (relevant & set(p.keys())):
        return []

    cur = SettingsManager.current()
    eff = {
        "auth_channels": p.get("auth_channels", cur.auth_channels),
        "manual_channels": p.get("manual_channels", cur.manual_channels),
        "anime_channels": p.get("anime_channels", cur.anime_channels),
        "multi_tokens": p.get("multi_tokens", cur.multi_tokens),
        "announcement_channel": p.get("announcement_channel", cur.announcement_channel),
    }
    #----- Only run the (costly) live checks when a relevant value actually changed
    def _norm(v):
        return [str(x).strip() for x in v] if isinstance(v, list) else str(v or "").strip()
    if all(_norm(eff[k]) == _norm(getattr(cur, k)) for k in eff):
        return []
    p = eff

    full: List[str] = []
    for key in ("auth_channels", "manual_channels", "anime_channels"):
        for ch in _list(p, key):
            ch = str(ch).strip()
            if _channel_ok(ch) and ch not in full:
                full.append(ch)
    ann = str(p.get("announcement_channel") or "").strip()
    ann = ann if _channel_ok(ann) else ""

    if not full and not ann:
        return []

    e: List[str] = []
    bots, temps = await _resolve_bots(p)
    try:
        for name, client in bots:
            if client is None:
                e.append(f"{name} token is invalid — it couldn't start, so it can't be verified.")
        live_bots = [(n, c) for n, c in bots if c is not None]
        if not live_bots:
            return e or ["Can't verify channels — no working bot is available."]

        for ch in full:
            cid = _to_chat_id(ch)
            for name, client in live_bots:
                ok, reason = await _admin_write(client, cid)
                if not ok:
                    e.append(f"Channel {ch}: {name} {reason}.")

        if ann:
            name, client = live_bots[0]
            ok, reason = await _admin_write(client, _to_chat_id(ann))
            if not ok:
                e.append(f"Announcement channel {ann}: {name} {reason}.")
    finally:
        for c in temps:
            try:
                await c.stop()
            except Exception:
                pass
    return e
