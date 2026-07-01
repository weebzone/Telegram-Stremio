from pyrogram import Client

from Backend.config import Telegram

#----- Primary bot client (loads plugins)
StreamBot = Client(
    name='bot',
    api_id=Telegram.API_ID,
    api_hash=Telegram.API_HASH,
    bot_token=Telegram.BOT_TOKEN,
    plugins={"root": "Backend/pyrofork/plugins"},
    sleep_threshold=20,
    workers=6,
    max_concurrent_transmissions=10
)

USERBOT_CLIENT_INDEX = -1

#----- Optional userbot client (only when a session string is configured)
Userbot = None
if Telegram.USER_SESSION_STRING:
    Userbot = Client(
        name='userbot',
        api_id=Telegram.API_ID,
        api_hash=Telegram.API_HASH,
        session_string=Telegram.USER_SESSION_STRING,
        sleep_threshold=20,
        workers=6,
        max_concurrent_transmissions=10,
        no_updates=True,
    )

#----- Shared multi-client registries
multi_clients = {}
work_loads = {}
client_dc_map = {}
client_failures = {}
client_avg_mbps = {}


#----- Resolve the bot's public t.me URL from cached username/me
def get_streambot_url() -> str:
    try:
        username = getattr(StreamBot, "username", None)
        if not username:
            me = getattr(StreamBot, "me", None)
            username = getattr(me, "username", None) if me else None
        if username:
            return f"https://t.me/{username}"
    except Exception:
        pass
    return "https://t.me/"


#----- Per-client workload map ({botN: load}), busiest first; {} when idle
def work_loads_summary() -> dict:
    if not work_loads:
        return {}
    return {
        f"bot{c + 1}": load
        for c, (_, load) in enumerate(sorted(work_loads.items(), key=lambda x: x[1], reverse=True))
    }


if Userbot is not None:
    work_loads[USERBOT_CLIENT_INDEX] = 0
    client_failures[USERBOT_CLIENT_INDEX] = 0
    client_avg_mbps[USERBOT_CLIENT_INDEX] = 0.0
