"""One-off: list files sitting in the skip channel (name + caption) into skipped.txt.

Run from the repo root on the server, before the phone-login/Userbot session is removed:
    uv run python scripts/dump_skipped.py            # skip channel read from DB settings
    uv run python scripts/dump_skipped.py -100123..  # or pass the channel id
Read-only: it never sends, deletes or edits anything.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `Backend` imports

from pyrogram import Client

from Backend import db
from Backend.config import Telegram
from Backend.helper.session_auth import get_active_session_string


async def main():
    await db.connect()
    chat = sys.argv[1] if len(sys.argv) > 1 else (await db.get_settings()).get("skip_channel")
    if not chat:
        sys.exit("No skip channel in settings. Pass the channel id as an argument.")
    session = await get_active_session_string()
    if not session:
        sys.exit("No stored Userbot session. Log in with the phone in the admin tab first.")
    chat = int(chat) if str(chat).lstrip("-").isdigit() else chat

    count = 0
    async with Client("dump", api_id=Telegram.API_ID, api_hash=Telegram.API_HASH,
                      session_string=session, in_memory=True, no_updates=True) as app:
        await app.get_chat(chat)  # warm the peer cache so the id resolves
        with open("skipped.txt", "w", encoding="utf-8") as out:
            async for m in app.get_chat_history(chat):
                media = m.document or m.video
                if not media:
                    continue
                out.write(f"{media.file_name or ''}\t{(m.caption or '').replace(chr(10), ' ')}\n")
                count += 1
    print(f"Wrote {count} lines to skipped.txt")


asyncio.run(main())
