from asyncio import sleep
from pyrogram.errors import FloodWait
from Backend.logger import LOGGER
from Backend.pyrofork.bot import Helper

async def edit_message(chat_id: int, msg_id: int, new_caption: str):
    try:
        await Helper.edit_message_caption(
            chat_id=chat_id,
            message_id=msg_id,
            caption=new_caption
        )
        await sleep(2)
    except FloodWait as e:
        LOGGER.warning(f"FloodWait for {e.value} seconds while editing message {msg_id} in {chat_id}")
        await sleep(e.value)
    except Exception as e:
        LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")

async def delete_message(chat_id: int, msg_id: int):
    try:
        await Helper.delete_messages(
            chat_id=chat_id,
            message_ids=msg_id
        )
        await sleep(2)
        LOGGER.info(f"Deleted message {msg_id} in {chat_id}")
    except FloodWait as e:
        LOGGER.warning(f"FloodWait for {e.value} seconds while deleting message {msg_id} in {chat_id}")
        await sleep(e.value)
    except Exception as e:
        LOGGER.error(f"Error while deleting message {msg_id} in {chat_id}: {e}")
