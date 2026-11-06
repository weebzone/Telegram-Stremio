"""One-off: list files sitting in the skip channel (name + caption) into skipped.txt.

Uses the main bot token (bots can read messages by id in channels they belong to), so no
phone login is needed. Run from the repo root on the server:
    uv run python scripts/dump_skipped.py              # skip channel read from DB settings
    uv run python scripts/dump_skipped.py -100123...   # or pass the channel id
Read-only: it never sends, deletes or edits anything.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `Backend` imports

from pyrogram import Client

from Backend import db
from Backend.config import Telegram

BATCH = 200
EMPTY_BATCHES_TO_STOP = 5  # this many all-empty batches in a row = end of channel


async def main():
    await db.connect()
    chat = sys.argv[1] if len(sys.argv) > 1 else (await db.get_settings()).get("skip_channel")
    if not chat:
        sys.exit("No skip channel in settings. Pass the channel id as an argument.")
    chat = int(chat) if str(chat).lstrip("-").isdigit() else chat

    count = scanned = empty_run = 0
    async with Client("dump", api_id=Telegram.API_ID, api_hash=Telegram.API_HASH,
                      bot_token=Telegram.BOT_TOKEN, in_memory=True, no_updates=True) as app:
        with open("skipped.txt", "w", encoding="utf-8") as out:
            first = 1
            while empty_run < EMPTY_BATCHES_TO_STOP:
                msgs = await app.get_messages(chat, list(range(first, first + BATCH)))
                found = [m for m in msgs if not m.empty]
                empty_run = 0 if found else empty_run + 1
                for m in found:
                    media = m.document or m.video
                    if media:
                        out.write(f"{media.file_name or ''}\t{(m.caption or '').replace(chr(10), ' ')}\n")
                        count += 1
                scanned += BATCH
                first += BATCH
                print(f"scanned {scanned} ids, {count} files", end="\r")
    print(f"\nWrote {count} lines to skipped.txt")


asyncio.run(main())
