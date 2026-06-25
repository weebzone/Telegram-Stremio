from pyrogram import Client
from Backend.config import Telegram

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

multi_clients = {}
work_loads = {}
client_dc_map = {}
client_failures = {}
client_avg_mbps = {}


def get_streambot_url() -> str:
    """Public ``t.me`` link of the Stream bot.

    Used everywhere a user needs to be pointed back to the bot (renew an
    expired plan, join the subscription channel, etc.). ``StreamBot.username``
    is populated at startup in ``Backend/__main__.py`` after the client starts.
    Falls back to ``https://t.me/`` if the username is not yet available.
    """
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

if Userbot is not None:
    work_loads[USERBOT_CLIENT_INDEX] = 0
    client_failures[USERBOT_CLIENT_INDEX] = 0
    client_avg_mbps[USERBOT_CLIENT_INDEX] = 0.0
