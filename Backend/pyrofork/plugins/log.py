from os import path as ospath

from pyrogram import Client, filters
from pyrogram.types import Message

from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER


#----- Owner-only /log: send the log file as a document
@Client.on_message(filters.command('log') & filters.private & CustomFilters.owner, group=10)
async def log(client: Client, message: Message):
    try:
        path = ospath.abspath('log.txt')
        if not ospath.exists(path):
            return await message.reply_text("> ❌ Log file not found.")

        await message.reply_document(document=path, quote=True, disable_notification=True)
    except Exception as e:
        await message.reply_text(f"⚠️ Error: {e}")
        LOGGER.error(f"Error in /log: {e}")
