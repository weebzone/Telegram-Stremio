from pyrogram import Client, filters
from pyrogram.types import ChatMemberUpdated
from pyrogram.enums import ChatMemberStatus
from Backend import db
from datetime import datetime
from Backend.logger import LOGGER
from Backend.helper.settings_manager import SettingsManager

@Client.on_chat_member_updated()
async def on_user_join(client: Client, chat_member_updated: ChatMemberUpdated):
    if not SettingsManager.current().subscription:
        return

    if chat_member_updated.chat.id != SettingsManager.current().subscription_group_id:
        return

    old_status = chat_member_updated.old_chat_member.status if chat_member_updated.old_chat_member else None
    new_status = chat_member_updated.new_chat_member.status if chat_member_updated.new_chat_member else None
    
    if new_status == ChatMemberStatus.MEMBER:
        user = chat_member_updated.new_chat_member.user
        db_user = await db.get_user(user.id)
        is_active = False
        if db_user and db_user.get("subscription_status") == "active":
            if db_user.get("subscription_expiry") and db_user.get("subscription_expiry") > datetime.utcnow():
                is_active = True
                
        if not is_active:
            try:
                await client.ban_chat_member(chat_member_updated.chat.id, user.id)
                await client.unban_chat_member(chat_member_updated.chat.id, user.id)
                LOGGER.info(f"Kicked unauthorized user {user.id} from group {chat_member_updated.chat.id}")
                await client.send_message(
                    user.id,
                    "❌ <b>Access Denied</b>\n\n"
                    "You were removed from the private group because you do not have an active subscription.\n"
                    "Please press /start in this bot to purchase or renew your subscription."
                )
            except Exception as e:
                LOGGER.error(f"Failed to kick/notify unauthorized user {user.id}: {e}")
