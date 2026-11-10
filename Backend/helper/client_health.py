"""Keep every bot's Telegram connections alive.

After a network blip (or an idle TCP connection dropped by the cloud NAT) a Pyrogram session can stay
on a dead socket: every request then fails with "Broken pipe" and asyncio logs
"socket.send() raised exception." forever. This module pings each session and reconnects dead ones.
"""
import asyncio

from pyrogram import raw

from Backend.logger import LOGGER

CHECK_EVERY = 60      # seconds between checks
PING_TIMEOUT = 15     # seconds a ping may take before the connection counts as dead

_healing: dict = {}   # client index -> running heal task, so one client restarts once at a time


async def _alive(session) -> bool:
    try:
        await asyncio.wait_for(session.send(raw.functions.Ping(ping_id=0)), PING_TIMEOUT)
        return True
    except Exception:
        return False


async def _stop_quietly(session) -> None:
    try:
        await session.stop()
    except Exception:
        pass


#----- Reconnect a client's main session. Concurrent callers share one restart.
async def heal(idx, client) -> None:
    task = _healing.get(idx)
    if task is None or task.done():
        task = asyncio.ensure_future(_restart(idx, client))
        _healing[idx] = task
    await asyncio.shield(task)


async def _restart(idx, client) -> None:
    LOGGER.warning(f"[Health] Bot {idx} lost its Telegram connection, reconnecting")
    try:
        await asyncio.wait_for(client.session.restart(), 120)
        LOGGER.info(f"[Health] Bot {idx} reconnected")
    except Exception as e:
        LOGGER.error(f"[Health] Bot {idx} reconnect failed, will retry: {e}")


#----- Fire-and-forget heal, for request paths that just saw a connection error
def schedule_heal(idx, client) -> None:
    asyncio.ensure_future(heal(idx, client))


#----- True when the main connection answered. Dead media (download) sessions are dropped;
#----- ByteStreamer reopens them on the next play.
async def check_client(idx, client) -> bool:
    for dc, session in list(getattr(client, "media_sessions", {}).items()):
        if not await _alive(session):
            client.media_sessions.pop(dc, None)
            asyncio.ensure_future(_stop_quietly(session))
            LOGGER.info(f"[Health] Bot {idx}: dropped dead DC{dc} media connection")
    if await _alive(client.session):
        return True
    await heal(idx, client)
    return False


async def watchdog(clients: dict) -> None:
    while True:
        await asyncio.sleep(CHECK_EVERY)
        for idx, client in list(clients.items()):
            try:
                await check_client(idx, client)
            except Exception as e:
                LOGGER.warning(f"[Health] Check of bot {idx} failed: {e}")
