from pyrogram.filters import create

from Backend.config import Telegram


#----- Pyrogram filters restricting handlers to the configured owner
class CustomFilters:

    @staticmethod
    async def owner_filter(client, message):
        user = message.from_user or message.sender_chat
        return user.id == Telegram.OWNER_ID

    owner = create(owner_filter)
