from pyrogram import Client, filters
from pyrogram.types import ChatMemberUpdated
from Backend.helper.settings_manager import SettingsManager
from Backend import db
from datetime import datetime
from Backend.logger import LOGGER
from pyrogram.enums import ChatMemberStatus

config = SettingsManager.current()

@Client.on_chat_member_updated()
async def on_user_join(client: Client, chat_member_updated: ChatMemberUpdated):
    if not config.subscription:
        return

    # Check if this update is for the subscription group
    if chat_member_updated.chat.id != config.subscription_group_id:
        return

    # Check if it's a join event (user was not a member, now is a member)
    old_status = chat_member_updated.old_chat_member.status if chat_member_updated.old_chat_member else None
    new_status = chat_member_updated.new_chat_member.status if chat_member_updated.new_chat_member else None
    
    if new_status == ChatMemberStatus.MEMBER:
        user = chat_member_updated.new_chat_member.user
        
        # Verify subscription
        db_user = await db.get_user(user.id)
        is_active = False
        if db_user and db_user.get("subscription_status") == "active":
            if db_user.get("subscription_expiry") and db_user.get("subscription_expiry") > datetime.utcnow():
                is_active = True
                
        if not is_active:
            try:
                # Kick the user
                await client.ban_chat_member(chat_member_updated.chat.id, user.id)
                await client.unban_chat_member(chat_member_updated.chat.id, user.id)
                LOGGER.info(f"Kicked unauthorized user {user.id} from group {chat_member_updated.chat.id}")
                
                # Notify them
                await client.send_message(
                    user.id,
                    "❌ <b>Access Denied</b>\n\n"
                    "You were removed from the private group because you do not have an active subscription.\n"
                    "Please press /start in this bot to purchase or renew your subscription."
                )
            except Exception as e:
                LOGGER.error(f"Failed to kick/notify unauthorized user {user.id}: {e}")
