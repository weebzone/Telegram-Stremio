from pyrogram import Client, filters
from pyrogram.types import Message
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.custom_filter import CustomFilters



@Client.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    if SettingsManager.current().subscription:
        text = (
            "<b>Bot Commands:</b>\n\n"
            "/start - Main menu / Purchase membership\n"
            "/status - Check your subscription expiry date\n"
            "/help - Show this message"
        )
    else:
        text = (
            "<b>Bot Commands:</b>\n\n"
            "/start - Get the Stremio Addon URL\n"
            "/help - Show this message"
        )
        
    await message.reply_text(text, quote=True)
