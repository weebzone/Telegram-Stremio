"""
Channel Manager Plugin — add, remove, and list AUTH_CHANNELs via the bot.

Commands (owner-only, private chat):
    /channels               — List all active AUTH_CHANNELs with names
    /addchannel <id>        — Add a channel (validates bot access first)
    /removechannel <id>     — Remove a channel

Changes take effect immediately (no restart needed) and are persisted to MongoDB.

"""

from pyrogram import filters, Client, enums
from pyrogram.types import Message
from pyrogram.errors import ChannelPrivate, ChatAdminRequired, PeerIdInvalid

from Backend import db
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER
from Backend.config import Telegram


# ── DB persistence helpers ───────────────────────────────────────────────

CHANNEL_DOC_ID = "auth_channels"


async def _load_channels_from_db():
    try:
        doc = await db.dbs["tracking"]["config"].find_one({"_id": CHANNEL_DOC_ID})
        if doc and "channels" in doc:
            db_channels = doc["channels"]
            for ch in db_channels:
                if ch not in Telegram.AUTH_CHANNEL:
                    Telegram.AUTH_CHANNEL.append(ch)
            LOGGER.info(
                f"[ChannelMgr] Loaded {len(db_channels)} channels from DB, "
                f"total active: {len(Telegram.AUTH_CHANNEL)}"
            )
    except Exception as e:
        LOGGER.error(f"[ChannelMgr] Failed to load channels from DB: {e}")


async def _save_channels_to_db():
    try:
        await db.dbs["tracking"]["config"].update_one(
            {"_id": CHANNEL_DOC_ID},
            {"$set": {"channels": list(Telegram.AUTH_CHANNEL)}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.error(f"[ChannelMgr] Failed to save channels to DB: {e}")


# ── Bot Commands ─────────────────────────────────────────────────────────

@Client.on_message(filters.command('channels') & filters.private & CustomFilters.owner, group=10)
async def list_channels(client: Client, message: Message):
    if not Telegram.AUTH_CHANNEL:
        await message.reply_text(
            "📭 No AUTH_CHANNELs configured.\n\n"
            "Use /addchannel <code>&lt;channel_id&gt;</code> to add one.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    lines = []
    for i, ch_id in enumerate(Telegram.AUTH_CHANNEL, 1):
        name = ch_id
        try:
            chat = await client.get_chat(int(ch_id))
            name = getattr(chat, "title", ch_id)
            members = getattr(chat, "members_count", "?")
            lines.append(f"{i}. <b>{name}</b>\n   ID: <code>{ch_id}</code> | Members: {members}")
        except Exception:
            lines.append(f"{i}. <b>[inaccessible]</b>\n   ID: <code>{ch_id}</code>")

    text = (
        "<blockquote>📡 <b>Active AUTH_CHANNELs</b></blockquote>\n\n"
        + "\n\n".join(lines)
        + "\n\n<i>Use /addchannel or /removechannel to manage.</i>"
    )
    await message.reply_text(text, quote=True, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command('addchannel') & filters.private & CustomFilters.owner, group=10)
async def add_channel(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            "Usage: /addchannel <code>&lt;channel_id&gt;</code>\n\n"
            "Example: <code>/addchannel -1001234567890</code>\n\n"
            "💡 <i>To find a channel ID, forward a message from it to "
            "@userinfobot or check the channel's invite link.</i>",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    ch_id = args[1].strip()

    try:
        int(ch_id)
    except ValueError:
        await message.reply_text(
            f"❌ <code>{ch_id}</code> is not a valid channel ID.\n"
            "Channel IDs are numeric, usually starting with <code>-100</code>.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    if ch_id in Telegram.AUTH_CHANNEL:
        await message.reply_text(
            f"ℹ️ Channel <code>{ch_id}</code> is already in AUTH_CHANNELs.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    status_msg = await message.reply_text(
        "🔍 Validating channel access…",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        chat = await client.get_chat(int(ch_id))
        ch_name = getattr(chat, "title", ch_id)
    except (ChannelPrivate, ChatAdminRequired):
        await status_msg.edit_text(
            f"❌ <b>Access denied</b> to channel <code>{ch_id}</code>.\n\n"
            "Make sure the bot is added as an <b>admin</b> in the channel first.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    except PeerIdInvalid:
        await status_msg.edit_text(
            f"❌ Channel <code>{ch_id}</code> not found.\n"
            "Double-check the ID.",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Error accessing channel: <code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    Telegram.AUTH_CHANNEL.append(ch_id)
    await _save_channels_to_db()

    LOGGER.info(f"[ChannelMgr] Added channel {ch_id} ({ch_name})")

    await status_msg.edit_text(
        f"✅ <b>Channel added!</b>\n\n"
        f"📡 <b>{ch_name}</b>\n"
        f"ID: <code>{ch_id}</code>\n\n"
        f"The bot will now index new videos from this channel.\n"
        f"Use /scan <code>{ch_id}</code> to index existing content.",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command('removechannel') & filters.private & CustomFilters.owner, group=10)
async def remove_channel(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        if Telegram.AUTH_CHANNEL:
            lines = []
            for ch_id in Telegram.AUTH_CHANNEL:
                try:
                    chat = await client.get_chat(int(ch_id))
                    name = getattr(chat, "title", ch_id)
                except Exception:
                    name = "[inaccessible]"
                lines.append(f"• <code>{ch_id}</code> — {name}")

            await message.reply_text(
                "Usage: /removechannel <code>&lt;channel_id&gt;</code>\n\n"
                "<b>Current channels:</b>\n" + "\n".join(lines),
                quote=True,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await message.reply_text(
                "📭 No channels to remove.",
                quote=True,
            )
        return

    ch_id = args[1].strip()

    if ch_id not in Telegram.AUTH_CHANNEL:
        await message.reply_text(
            f"❌ Channel <code>{ch_id}</code> is not in AUTH_CHANNELs.",
            quote=True,
            parse_mode=enums.ParseMode.HTML,
        )
        return

    ch_name = ch_id
    try:
        chat = await client.get_chat(int(ch_id))
        ch_name = getattr(chat, "title", ch_id)
    except Exception:
        pass

    Telegram.AUTH_CHANNEL.remove(ch_id)
    await _save_channels_to_db()

    LOGGER.info(f"[ChannelMgr] Removed channel {ch_id} ({ch_name})")

    await message.reply_text(
        f"✅ <b>Channel removed!</b>\n\n"
        f"📡 <b>{ch_name}</b> (<code>{ch_id}</code>)\n\n"
        f"⚠️ Existing DB entries from this channel are <b>not</b> deleted.\n"
        f"Videos already indexed will still be streamable.\n"
        f"New videos in this channel will no longer be indexed.",
        quote=True,
        parse_mode=enums.ParseMode.HTML,
    )


