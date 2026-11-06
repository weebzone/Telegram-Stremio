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
from pyrogram.enums import ParseMode

from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot

DELETE_BATCH_SIZE = 10
_FALLBACK_WORTHY = (
    ChatAdminRequired,
    ChannelPrivate,
    MessageDeleteForbidden,
    MessageAuthorRequired,
    UserNotParticipant,
    RPCError,
)
_SESSION_DEAD = (AuthKeyUnregistered, SessionRevoked)

async def _resolve_peer(client, chat_id) -> bool:
    try:
        await client.get_chat(chat_id)
        return True
    except Exception as e:
        return False


async def edit_message(chat_id: int, msg_id: int, new_caption: str):
    try:
        await StreamBot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption, parse_mode=ParseMode.HTML)
        await sleep(2)
        return
    except FloodWait as e:
        LOGGER.warning(f"FloodWait for {e.value}s while editing message {msg_id} in {chat_id}")
        await sleep(e.value)
        try:
            await StreamBot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption, parse_mode=ParseMode.HTML)
            return
        except Exception as e2:
            LOGGER.error(f"Retry after FloodWait failed while editing {msg_id} in {chat_id}: {e2}")
    except PeerIdInvalid as e:
        if await _resolve_peer(StreamBot, chat_id):
            try:
                await StreamBot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=new_caption, parse_mode=ParseMode.HTML)
                return
            except Exception as e2:
                LOGGER.error(f"Retry after peer resolve failed while editing {msg_id} in {chat_id}: {e2}")
        LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")
    except _FALLBACK_WORTHY as e:
        LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")
    except Exception as e:
        LOGGER.error(f"Error while editing message {msg_id} in {chat_id}: {e}")


async def delete_message(chat_id: int, msg_id: int):
    await delete_messages_batch(chat_id, [msg_id])


async def delete_messages_batch(chat_id: int, msg_ids: List[int]):
    if not msg_ids:
        return

    for i in range(0, len(msg_ids), DELETE_BATCH_SIZE):
        chunk = msg_ids[i:i + DELETE_BATCH_SIZE]

        remaining = await _delete_chunk(StreamBot, "StreamBot", chat_id, chunk)

        if remaining:
            LOGGER.error(f"Could not delete {len(remaining)} message(s) in {chat_id}")

        await sleep(1)


async def _delete_chunk(client, client_label: str, chat_id: int, msg_ids: List[int]) -> List[int]:
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
    except PeerIdInvalid as e:
        if await _resolve_peer(client, chat_id):
            try:
                await client.delete_messages(chat_id=chat_id, message_ids=msg_ids)
                LOGGER.info(f"[{client_label.upper()}] Deleted {len(msg_ids)} message(s) in {chat_id} after peer resolve")
                return []
            except Exception as e2:
                LOGGER.error(f"[{client_label.upper()}] Retry after peer resolve failed in {chat_id}: {e2}")
                return msg_ids
        LOGGER.warning(f"[{client_label.upper()}] Peer unresolved in {chat_id}: {e}")
        return msg_ids
    except _SESSION_DEAD as e:
        LOGGER.error(f"[{client_label.upper()}] Session invalid ({type(e).__name__}): {e}")
        return msg_ids
    except _FALLBACK_WORTHY as e:
        LOGGER.warning(f"[{client_label.upper()}] Cannot delete in {chat_id} ({type(e).__name__}): {e}")
        return msg_ids
    except Exception as e:
        LOGGER.error(f"[{client_label.upper()}] Unexpected error deleting in {chat_id}: {e}")
        return msg_ids
