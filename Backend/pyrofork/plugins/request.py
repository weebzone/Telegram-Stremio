from pyrogram import Client, enums, filters
from pyrogram.types import Message

from Backend.helper.requests_manager import create_request, notify_approvers
from Backend.logger import LOGGER


#----- /request <title>: log a content request and notify approvers
@Client.on_message(filters.command("request") & filters.private, group=11)
async def handle_request(client: Client, message: Message):
    try:
        user = message.from_user
        user_id = (user.id if user else None) or message.chat.id
        parts = (message.text or "").split(maxsplit=1)
        title = parts[1].strip() if len(parts) > 1 else ""

        if not title:
            return await message.reply_text(
                "📝 <b>Request a title</b>\n\n"
                "Send it like:\n<code>/request Inception 2010</code>",
                quote=True, parse_mode=enums.ParseMode.HTML,
            )

        user_name = (user.first_name or user.username or f"User {user_id}") if user else f"Chat {user_id}"
        username = user.username if user else None
        await create_request(user_id, user_name, username, title)

        await message.reply_text(
            f"✅ <b>Request received!</b>\n\n🎬 <b>{title}</b>\n\n"
            "The admin has been notified — you'll hear back once it's added.",
            quote=True, parse_mode=enums.ParseMode.HTML,
        )

        uname = f"@{username}" if username else "N/A"
        await notify_approvers(
            f"🆕 <b>New Content Request</b>\n\n"
            f"🎬 <b>{title[:300]}</b>\n"
            f"👤 {user_name}\n🔗 {uname}\n🆔 <code>{user_id}</code>"
        )
    except Exception as e:
        LOGGER.error(f"[REQUEST] handler failed: {e}")
