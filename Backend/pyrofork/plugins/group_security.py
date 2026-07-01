from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import ChatMemberUpdated

from Backend import db
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Kick joiners of the subscription group who lack an active subscription
@Client.on_chat_member_updated()
async def on_user_join(client: Client, chat_member_updated: ChatMemberUpdated):
    if not SettingsManager.current().subscription:
        return
    if chat_member_updated.chat.id != SettingsManager.current().subscription_group_id:
        return

    new_status = chat_member_updated.new_chat_member.status if chat_member_updated.new_chat_member else None
    if new_status != ChatMemberStatus.MEMBER:
        return

    user = chat_member_updated.new_chat_member.user
    db_user = await db.get_user(user.id)
    if db.is_subscription_active(db_user):
        return

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
