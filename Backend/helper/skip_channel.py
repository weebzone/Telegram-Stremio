from asyncio import sleep as asleep

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


def is_skip_channel(message: Message) -> bool:
    skip = SettingsManager.current().skip_channel
    if not skip:
        return False
    ref = str(skip).strip()
    if ref.lstrip("@-").replace("-100", "").isdigit():
        return ref.replace("-100", "").lstrip("@") == str(message.chat.id).replace("-100", "")
    username = (getattr(message.chat, "username", None) or "").lower()
    return bool(username) and ref.lstrip("@").lower() == username


async def route_to_skip_channel(client: Client, message: Message) -> None:
    from Backend.helper.skipped_files import record_skipped
    await record_skipped(message)  # keep a dashboard record even without a skip channel
    settings = SettingsManager.current()
    skip = settings.skip_channel
    if not skip:
        return

    skip_chat = int(skip) if str(skip).lstrip("-").replace("-100", "").isdigit() else skip

    copied = None
    try:
        copied = await message.copy(skip_chat)
    except FloodWait as e:
        await asleep(e.value)
        try:
            copied = await message.copy(skip_chat)
        except Exception as e2:
            LOGGER.error(f"[SkipChannel] Copy failed for message {message.id}: {e2}")
            return
    except Exception as e:
        LOGGER.error(f"[SkipChannel] Could not copy message {message.id} to skip channel: {e}")
        return

    if settings.delete_on_metadata_fail:
        try:
            from Backend.helper.task_manager import delete_message
            await delete_message(message.chat.id, message.id)
            if copied is not None:  # the original is gone, so the dashboard must retry the copy
                from Backend.helper.skipped_files import retarget_to_copy
                await retarget_to_copy(message, copied)
        except Exception as e:
            LOGGER.warning(f"[SkipChannel] Could not delete original message {message.id}: {e}")
