from asyncio import sleep
from pyrogram.errors import FloodWait
from Backend.logger import LOGGER
from Backend.pyrofork.bot import Helper
import asyncio
from typing import Optional
from Backend.helper.subscription_checker import subscription_checker_loop
from Backend.helper.settings_manager import SettingsManager

_task: Optional[asyncio.Task] = None


def is_running() -> bool:
    return _task is not None and not _task.done()

async def start(bot) -> bool:
    global _task
    if is_running():
        return False
    _task = asyncio.create_task(subscription_checker_loop(bot))
    LOGGER.info("Subscription Checker Task Started.")
    return True


async def stop() -> bool:
    global _task
    if not is_running():
        _task = None
        return False

    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        LOGGER.warning(f"Error while stopping subscription checker task: {e}")
    finally:
        _task = None

    LOGGER.info("Subscription Checker Task Stopped.")
    return True


async def sync(bot) -> None:
    if SettingsManager.current().subscription:
        await start(bot)


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
