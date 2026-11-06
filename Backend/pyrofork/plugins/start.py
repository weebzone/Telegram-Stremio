from pyrogram import Client, enums, filters
from pyrogram.types import Message

from Backend import db
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.utils import addon_links_text
from Backend.logger import LOGGER


#----- /start: hand out the Stremio addon link to the owner
@Client.on_message(filters.command('start') & filters.private, group=10)
async def send_start_message(client: Client, message: Message):
    try:
        user_id = (message.from_user.id if message.from_user else None) or (message.sender_chat.id if message.sender_chat else None) or message.chat.id
        if user_id != Telegram.OWNER_ID:
            return
        addon_url = f"{SettingsManager.current().base_url}/stremio/manifest.json"
        user_name = (message.from_user.first_name or message.from_user.username or f"User {user_id}") if message.from_user else f"Chat {user_id}"
        try:
            token_doc = await db.add_api_token(name=user_name, user_id=user_id)
            links = addon_links_text(token_doc["token"])
        except Exception as e:
            LOGGER.error(f"Error ensuring token for owner: {e}")
            links = f"🧩 <b>Addon Manifest URL:</b>\n<code>{addon_url}</code>"

        await message.reply_text(
            '🎉 <b>Welcome to the Telegram Stremio Media Server!</b>\n\n'
            'Here are your personal links:\n\n' + links,
            quote=True,
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await message.reply_text(f"⚠️ Error: {e}")
        LOGGER.error(f"Error in /start handler: {e}")
