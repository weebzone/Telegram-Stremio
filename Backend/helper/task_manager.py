from asyncio import sleep
from typing import List

from pyrogram.errors import (
    FloodWait,
    ChatAdminRequired,
    ChannelPrivate,
    MessageDeleteForbidden,
    MessageAuthorRequired,
    PeerIdInvalid,
    UserNotParticipant,
    AuthKeyUnregistered,
    SessionRevoked,
    RPCError,
)

from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, Userbot

DELETE_BATCH_SIZE = 10
_FALLBACK_WORTHY = (
    ChatAdminRequired,
    ChannelPrivate,
    MessageDeleteForbidden,
    MessageAuthorRequired,
    PeerIdInvalid,
    UserNotParticipant,
    RPCError,
)
_SESSION_DEAD = (AuthKeyUnregistered, SessionRevoked)
_userbot_session_dead = False


def _userbot_usable() -> bool:
    return Userbot is not None and not _userbot_session_dead


#----- Edit a message caption via StreamBot, falling back to the Userbot
async def edit_message(chat_id: int, msg_id: int, new_caption: str):
    try:
        await StreamBot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption)
        await sleep(2)
        return
    except FloodWait as e:
        LOGGER.warning(f"FloodWait for {e.value}s while editing message {msg_id} in {chat_id}")
        await sleep(e.value)
        try:
            await StreamBot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption)
            return
        except Exception as e2:
            LOGGER.error(f"Retry after FloodWait failed while editing {msg_id} in {chat_id}: {e2}")
    except _FALLBACK_WORTHY as e:
        if not _userbot_usable():
            LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")
            return
        LOGGER.info(f"[USERBOT] Fallback triggered: edit_message {msg_id} in {chat_id} (StreamBot: {e})")
        await _userbot_edit(chat_id, msg_id, new_caption)
    except Exception as e:
        LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")


async def _userbot_edit(chat_id: int, msg_id: int, new_caption: str):
    global _userbot_session_dead
    try:
        await Userbot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption)
        await sleep(2)
    except FloodWait as e:
        LOGGER.warning(f"[USERBOT] FloodWait detected: sleeping {e.value}s (edit {msg_id} in {chat_id})")
        await sleep(e.value)
        try:
            await Userbot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption)
        except Exception as e2:
            LOGGER.error(f"[USERBOT] Retry after FloodWait failed while editing {msg_id} in {chat_id}: {e2}")
    except _SESSION_DEAD as e:
        _userbot_session_dead = True
        LOGGER.error(f"[USERBOT] Session invalid ({type(e).__name__}): {e}. Disabling Userbot fallback for this run.")
    except Exception as e:
        LOGGER.error(f"[USERBOT] Error while editing message {msg_id} in {chat_id}: {e}")


async def delete_message(chat_id: int, msg_id: int):
    await delete_messages_batch(chat_id, [msg_id])


#----- Delete messages in batches, using the Userbot fallback for leftovers
async def delete_messages_batch(chat_id: int, msg_ids: List[int]):
    if not msg_ids:
        return

    for i in range(0, len(msg_ids), DELETE_BATCH_SIZE):
        chunk = msg_ids[i:i + DELETE_BATCH_SIZE]

        remaining = await _delete_chunk(StreamBot, "StreamBot", chat_id, chunk)

        if remaining and _userbot_usable():
            LOGGER.info(f"[USERBOT] Fallback triggered: deleting {len(remaining)} message(s) in {chat_id}")
            remaining = await _delete_chunk(Userbot, "Userbot", chat_id, remaining)

        if remaining:
            LOGGER.error(
                f"Could not delete {len(remaining)} message(s) in {chat_id} "
                f"(no usable Userbot fallback)" if not _userbot_usable() else
                f"Could not delete {len(remaining)} message(s) in {chat_id} even with Userbot fallback"
            )

        await sleep(1)


async def _delete_chunk(client, client_label: str, chat_id: int, msg_ids: List[int]) -> List[int]:
    global _userbot_session_dead
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=msg_ids)
        LOGGER.info(f"[{client_label.upper()}] Deleted {len(msg_ids)} message(s) in {chat_id}")
        return []
    except FloodWait as e:
        LOGGER.warning(f"[{client_label.upper()}] FloodWait detected: sleeping {e.value}s ({len(msg_ids)} msg(s) in {chat_id})")
        await sleep(e.value)
        try:
            await client.delete_messages(chat_id=chat_id, message_ids=msg_ids)
            LOGGER.info(f"[{client_label.upper()}] Deleted {len(msg_ids)} message(s) in {chat_id} after FloodWait retry")
            return []
        except Exception as e2:
            LOGGER.error(f"[{client_label.upper()}] Retry after FloodWait failed in {chat_id}: {e2}")
            return msg_ids
    except _SESSION_DEAD as e:
        if client_label == "Userbot":
            _userbot_session_dead = True
        LOGGER.error(f"[{client_label.upper()}] Session invalid ({type(e).__name__}): {e}")
        return msg_ids
    except _FALLBACK_WORTHY as e:
        LOGGER.warning(f"[{client_label.upper()}] Cannot delete in {chat_id} ({type(e).__name__}): {e}")
        return msg_ids
    except Exception as e:
        LOGGER.error(f"[{client_label.upper()}] Unexpected error deleting in {chat_id}: {e}")
        return msg_ids
