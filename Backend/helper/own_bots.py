import asyncio
import re
from datetime import datetime, timedelta

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus

from Backend import db
from Backend.config import Telegram
from Backend.helper.bounded import BoundedDict
from Backend.helper.cf_stream import sync_worker_soon
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import client_dc_map, work_loads

TOKEN_RE = re.compile(r"^\d{5,}:[\w-]{30,}$")
MAX_BOTS = 10
COPY_TTL = timedelta(days=1)
TOUCH_EVERY = timedelta(hours=1)
STREAM_VIA = ("bots", "cloudflare", "both")

helpers: list = []
_pools: dict = {}
_known = BoundedDict(5000)
_next_index = 1000
_copy_lock = asyncio.Lock()
_pool_lock = asyncio.Lock()


def _tokens():
    return db.dbs["tracking"]["api_tokens"]


def _copies():
    return db.dbs["tracking"]["own_bot_copies"]


async def _start_helper(i: int, token: str):
    try:
        return await Client(
            name=f"helper{i}", api_id=Telegram.API_ID, api_hash=Telegram.API_HASH,
            bot_token=token, no_updates=True, in_memory=True,
        ).start()
    except Exception as e:
        LOGGER.error(f"Helper bot {i} failed to start: {e}")


async def reload_helpers() -> int:
    old = helpers[:]
    helpers.clear()
    await asyncio.gather(*(c.stop() for c in old), return_exceptions=True)
    started = await asyncio.gather(*(_start_helper(i, t) for i, t in enumerate(SettingsManager.current().helper_tokens, 1)))
    helpers.extend(c for c in started if c)
    return len(helpers)


def active(token_data: dict) -> bool:
    own = token_data.get("own_bots") or {}
    return bool(helpers and own.get("bots") and own.get("channel"))


def worker(token_data: dict):
    cf = token_data.get("cf") or {}
    return cf if active(token_data) and cf.get("url") and cf.get("secret") else None


def stream_via(token_data: dict):
    if not active(token_data):
        return None
    via = (token_data.get("config") or {}).get("stream_via")
    return via if worker(token_data) and via in STREAM_VIA else "bots"


def public(own: dict) -> dict:
    own = own or {}
    return {
        "channel": own.get("channel"),
        "bots": [{"id": b["id"], "username": b.get("username")} for b in own.get("bots") or []],
        "verified_at": own.get("verified_at"),
    }


def addon_view(token_data: dict) -> dict:
    return {
        "available": bool(helpers),
        "helpers": [h.me.username for h in helpers],
        "enabled": active(token_data),
        "cloudflare": bool(worker(token_data)),
        **public(token_data.get("own_bots")),
    }


def _sync(token_data: dict) -> None:
    cf = token_data.get("cf") or {}
    if cf.get("url") and cf.get("secret"):
        sync_worker_soon(cf["url"], cf["secret"])


async def _start_pool(bots: list) -> dict:
    global _next_index
    from Backend.pyrofork.clients import start_client
    ids = list(range(_next_index, _next_index + len(bots)))
    _next_index += len(bots)
    results = await asyncio.gather(*(start_client(i, b["token"]) for i, b in zip(ids, bots)))
    return dict(r for r in results if r)


async def _stop(pool: dict) -> None:
    from Backend.fastapi.routes.stream_routes import _streamer_by_client
    for idx, client in pool.items():
        work_loads.pop(idx, None)
        client_dc_map.pop(idx, None)
        _streamer_by_client.pop(client, None)
        try:
            await client.stop()
        except Exception as e:
            LOGGER.warning(f"Error stopping user bot {idx}: {e}")


async def stop_pool(token: str) -> None:
    await _stop(_pools.pop(token, {}))


async def pool_for(token_data: dict):
    token = token_data["token"]
    async with _pool_lock:
        if token not in _pools:
            pool = await _start_pool(token_data["own_bots"]["bots"])
            if not pool:
                return None
            _pools[token] = pool
    return _pools[token]


def _full_chat(chat) -> int:
    chat = int(chat)
    return int(f"-100{chat}") if chat > 0 else chat


async def _copy(token: str, channel: int, chat: int, msg: int) -> int:
    key = f"{token}:{channel}:{chat}:{msg}"
    now = datetime.utcnow()
    if key in _known:
        copied, touched = _known[key]
        if now - touched > TOUCH_EVERY:
            _known[key] = (copied, now)
            asyncio.create_task(_copies().update_one({"_id": key}, {"$set": {"last_used": now}}))
        return copied
    doc = await _copies().find_one_and_update({"_id": key}, {"$set": {"last_used": now}})
    if doc:
        _known[key] = (doc["msg"], now)
        return doc["msg"]
    async with _copy_lock:
        doc = await _copies().find_one({"_id": key})
        if doc:
            _known[key] = (doc["msg"], now)
            return doc["msg"]
        error = RuntimeError("No helper bot available")
        for helper in helpers:
            try:
                copied = await helper.copy_message(channel, chat, msg)
                break
            except Exception as e:
                error = e
        else:
            raise error
        await _copies().insert_one({"_id": key, "token": token, "channel": channel, "msg": copied.id, "last_used": now})
        _known[key] = (copied.id, now)
        return copied.id


async def remap(token_data: dict, decoded: dict) -> dict:
    channel = int(token_data["own_bots"]["channel"])
    raw = str(channel)[4:]
    parts = decoded["parts"] if "parts" in decoded else [decoded]
    moved = [
        {**p, "chat_id": raw, "msg_id": await _copy(token_data["token"], channel, _full_chat(p["chat_id"]), int(p["msg_id"]))}
        for p in parts
    ]
    return {**decoded, "parts": moved} if "parts" in decoded else moved[0]


async def _sweep() -> None:
    if not helpers:
        return
    stale = await _copies().find({"last_used": {"$lt": datetime.utcnow() - COPY_TTL}}).to_list(None)
    by_channel: dict = {}
    for d in stale:
        by_channel.setdefault(d["channel"], []).append(d)
    for channel, docs in by_channel.items():
        ids = [d["msg"] for d in docs]
        for helper in helpers:
            try:
                for i in range(0, len(ids), 100):
                    await helper.delete_messages(channel, ids[i:i + 100])
                break
            except Exception as e:
                LOGGER.warning(f"[OWN BOTS] Could not delete copies in {channel}: {e}")
        await _copies().delete_many({"_id": {"$in": [d["_id"] for d in docs]}})
        for d in docs:
            _known.pop(d["_id"], None)


async def sweep_copies() -> None:
    await _copies().create_index("last_used")
    while True:
        await asyncio.sleep(3600)
        try:
            await _sweep()
        except Exception as e:
            LOGGER.warning(f"[OWN BOTS] Sweep failed: {e}")


async def warm_pools() -> None:
    async for doc in _tokens().find({"own_bots.bots.0": {"$exists": True}}):
        if active(doc):
            await pool_for(doc)


def _reason(e: Exception) -> str:
    s = str(e)
    if any(k in s for k in ("CHANNEL_PRIVATE", "CHANNEL_INVALID", "PEER_ID_INVALID", "USER_NOT_PARTICIPANT")):
        return "not added to the channel"
    return s[:160]


def _parse(token_data: dict, channel, keep: list, add: list) -> tuple:
    if not helpers:
        raise ValueError("Own bots are not enabled on this server")
    try:
        channel = int(str(channel).strip())
    except ValueError:
        raise ValueError("Channel ID must look like -100xxxxxxxxxx")
    if not str(channel).startswith("-100"):
        raise ValueError("Channel ID must look like -100xxxxxxxxxx")
    known = {b["id"]: b for b in (token_data.get("own_bots") or {}).get("bots") or []}
    bots = [dict(known[int(i)]) for i in keep if str(i).isdigit() and int(i) in known]
    for t in add:
        t = str(t).strip()
        if not TOKEN_RE.match(t):
            raise ValueError(f"Invalid bot token: {t[:12]}…")
        bots.append({"id": int(t.split(":")[0]), "token": t})
    bots = list({b["id"]: b for b in bots}.values())
    if not bots:
        raise ValueError("Add at least one bot")
    if len(bots) > MAX_BOTS:
        raise ValueError(f"At most {MAX_BOTS} bots")
    return channel, bots


async def verify(token_data: dict, channel, keep: list, add: list) -> dict:
    channel, bots = _parse(token_data, channel, keep, add)
    checks = []

    def check(label: str, ok, detail: str = "") -> None:
        checks.append({"label": label, "ok": bool(ok), "detail": "" if ok else detail})

    for h in helpers:
        try:
            m = await h.get_chat_member(channel, "me")
            p = m.privileges
            check(f"Helper @{h.me.username}", m.status == ChatMemberStatus.ADMINISTRATOR and p and p.can_post_messages and p.can_delete_messages,
                  "needs admin with Post and Delete messages rights")
        except Exception as e:
            check(f"Helper @{h.me.username}", False, _reason(e))

    pool = await _start_pool(bots)
    started = {c.me.id: c for c in pool.values()}
    for b in bots:
        c = started.get(b["id"])
        if not c:
            check(f"Bot {b['id']}", False, "token is invalid or revoked")
            continue
        b["username"] = c.me.username
        try:
            m = await c.get_chat_member(channel, "me")
            check(f"Bot @{c.me.username}", m.status == ChatMemberStatus.ADMINISTRATOR, "not an admin in the channel")
        except Exception as e:
            check(f"Bot @{c.me.username}", False, _reason(e))

    if all(c["ok"] for c in checks):
        probe = None
        try:
            probe = await helpers[0].send_message(channel, "Verifying bots…")
            for c in started.values():
                got = await c.get_messages(channel, probe.id)
                check(f"@{c.me.username} can read the channel", got and not got.empty, "cannot read channel messages")
        except Exception as e:
            check("Test message", False, _reason(e))
        finally:
            if probe:
                await helpers[0].delete_messages(channel, probe.id)

    ok = all(c["ok"] for c in checks)
    own = token_data.get("own_bots")
    if ok:
        own = {"channel": channel, "bots": bots, "verified_at": datetime.utcnow()}
        await _tokens().update_one({"token": token_data["token"]}, {"$set": {"own_bots": own}})
        await stop_pool(token_data["token"])
        _pools[token_data["token"]] = pool
        _sync(token_data)
    else:
        await _stop(pool)
    return {"ok": ok, "checks": checks, "own_bots": public(own)}


async def disable(token_data: dict) -> None:
    await _tokens().update_one({"token": token_data["token"]}, {"$unset": {"own_bots": ""}})
    await stop_pool(token_data["token"])
    _sync(token_data)


async def set_worker(token: str, url: str, secret: str) -> None:
    url, secret = (url or "").strip().rstrip("/"), (secret or "").strip()
    if url and not url.startswith("https://"):
        raise ValueError("Worker URL must start with https://")
    cf = {"url": url, "secret": secret} if url and secret else None
    await _tokens().update_one({"token": token}, {"$set": {"cf": cf}})
    if cf:
        sync_worker_soon(url, secret)


async def worker_tokens() -> list:
    return await _tokens().find({"cf.secret": {"$nin": [None, ""]}}, {"token": 1, "cf": 1, "own_bots": 1}).to_list(None)
