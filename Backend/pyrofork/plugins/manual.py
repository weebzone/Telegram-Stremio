from pyrogram import Client, enums, filters
from pyrogram.types import Message

import Backend
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER


#----- Owner-only /set: set or clear the default IMDB/TMDB URL for uploads
@Client.on_message(filters.command('set') & filters.private & CustomFilters.owner, group=10)
async def manual(client: Client, message: Message):
    try:
        command = message.text.split(maxsplit=1)

        if len(command) == 2:
            Backend.USE_DEFAULT_ID = command[1].strip()
            await message.reply_text(
                f"✅ <b>Default IMDB/TMDB URL Set!</b>\n\n"
                f"Now the bot will use this URL for any files you send:\n"
                f"<code>{Backend.USE_DEFAULT_ID}</code>\n\n"
                f"<b>Instructions:</b>\n"
                f"1. Forward the related movie or TV show files to your channel.\n"
                f"2. Once all files are uploaded, clear the default URL by sending <code>/set</code> without any URL.",
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )
        else:
            Backend.USE_DEFAULT_ID = None
            await message.reply_text(
                "✅ <b>Default IMDB/TMDB URL Removed!</b>\n\n"
                "You can now manually upload files without linking to a default IMDB URL.",
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )

    except Exception as e:
        LOGGER.error(f"Error in /set handler: {e}")
        await message.reply_text(f"⚠️ An error occurred: {e}")
