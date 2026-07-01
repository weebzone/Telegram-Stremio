from pyrogram.file_id import FileId
from typing import Optional
from Backend.logger import LOGGER
from Backend import __version__, now, timezone
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.exceptions import FileNotFound
from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, remove as aioremove
from pyrogram import Client
from Backend.pyrofork.bot import StreamBot
import re
from pyrogram.types import BotCommand
from pyrogram import enums


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"   #----- emoticons
    "\U0001F300-\U0001F5FF"   #----- symbols & pictographs
    "\U0001F680-\U0001F6FF"   #----- transport & map
    "\U0001F700-\U0001FAFF"   #----- extended symbols (geometric, supplemental, etc.)
    "\U00002702-\U000027B0"   #----- dingbats
    "\U000024C2-\U0001F251"   #----- enclosed chars
    "\u2600-\u26FF"           #----- misc symbols (☆, ★, ☀, ©, ®, ™, …)
    "\u2700-\u27BF"           #----- dingbats block
    "\uFE00-\uFE0F"           #----- variation selectors
    "\U0001F1E0-\U0001F1FF"   #----- regional indicator / flag sequences
    "]+",
    re.UNICODE,
)

_DECORATION_PATTERN = re.compile(
    r"[•·‣⁃◦⦿⦾❖✦✧✪✫✬✭✮★☆♦♣♠♥►◄→←↑↓»«|¦‖⟨⟩⌨⌚⌛⏰⏱⏲⏳✔✖✗✘✅❌❎⚠⛔🚫]+",
    re.UNICODE,
)

#----- Telegram / social-media channel tag patterns
_CHANNEL_TAG_PATTERN = re.compile(
    r"_@[A-Za-z]+_|@[A-Za-z]+_|[\[\]\s@]*@[^.\s\[\]]+[\]\[\s@]*"
)

_CODEC_TAG_PATTERN = re.compile(
    r"(?<=\W)(org|AMZN|DDP|DD|NF|AAC|TVDL|5\.1|2\.1|2\.0|7\.0|7\.1|5\.0|~|\b\w+kbps\b)(?=\W)",
    re.IGNORECASE,
)


def is_media(message):
    return next(
        (
            getattr(message, attr)
            for attr in [
                "document", "photo", "video", "audio",
                "voice", "video_note", "sticker", "animation",
            ]
            if getattr(message, attr)
        ),
        None,
    )


async def get_file_ids(client: Client, chat_id: int, message_id: int) -> Optional[FileId]:
    try:
        message = await client.get_messages(chat_id, message_id)
        if message.empty:
            raise FileNotFound("Message not found or empty")

        if media := is_media(message):
            file_id_obj = FileId.decode(media.file_id)
            file_unique_id = media.file_unique_id

            setattr(file_id_obj, 'file_name', getattr(media, 'file_name', ''))
            setattr(file_id_obj, 'file_size', getattr(media, 'file_size', 0))
            setattr(file_id_obj, 'mime_type', getattr(media, 'mime_type', ''))
            setattr(file_id_obj, 'unique_id', file_unique_id)

            return file_id_obj
        else:
            raise FileNotFound("No supported media found in message")
    except Exception as e:
        LOGGER.error(f"Error getting file IDs: {e}")
        raise


def get_readable_file_size(size_in_bytes):
    size_in_bytes = int(size_in_bytes) if str(size_in_bytes).isdigit() else 0
    if not size_in_bytes:
        return '0B'

    index, SIZE_UNITS = 0, ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1

    return f'{size_in_bytes:.2f}{SIZE_UNITS[index]}' if index > 0 else f'{size_in_bytes:.0f}B'


def clean_filename(filename: str) -> str:
    if not filename:
        return "unknown_file"

    #----- 1 – Remove emoji sequences
    filename = _EMOJI_PATTERN.sub(" ", filename)

    #----- 2 – Remove decorative unicode symbols
    filename = _DECORATION_PATTERN.sub(" ", filename)

    #----- 3 – Replace any remaining non-ASCII characters with a space.
    #----- Keep standard filename-safe characters: alphanumerics, . - _ ( ) [ ] ' " , : ! ? & + @
    filename = re.sub(r"[^\x20-\x7E]", " ", filename)

    #----- 4 – Remove Telegram channel tags  (@ChannelName_ etc.)
    filename = _CHANNEL_TAG_PATTERN.sub("", filename)

    #----- 5 – Remove codec / source tags that clutter the title region
    filename = _CODEC_TAG_PATTERN.sub(" ", filename)

    #----- 6 – Collapse multiple spaces; remove space before extension dot
    filename = re.sub(r"\s+", " ", filename).strip().replace(" .", ".")

    return filename if filename else "unknown_file"


def get_readable_time(seconds: int) -> str:
    count = 0
    readable_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", " days"]

    while count < 4:
        count += 1
        if count < 3:
            remainder, result = divmod(seconds, 60)
        else:
            remainder, result = divmod(seconds, 24)

        if seconds == 0 and remainder == 0:
            break

        time_list.append(int(result))
        seconds = int(remainder)

    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]

    if len(time_list) == 4:
        readable_time += time_list.pop() + ", "

    time_list.reverse()
    readable_time += ": ".join(time_list)

    return readable_time


def remove_urls(text):
    if not text:
        return ""

    url_pattern = r'\b(?:https?|ftp):\/\/[^\s/$.?#].[^\s]*'
    text_without_urls = re.sub(url_pattern, '', text)
    cleaned_text = re.sub(r'\s+', ' ', text_without_urls).strip()

    return cleaned_text


async def restart_notification():
    chat_id, msg_id = 0, 0
    try:
        if await aiopath.exists(".restartmsg"):
            async with aiopen(".restartmsg", "r") as f:
                data = await f.readlines()
                chat_id, msg_id = map(int, data)

            try:
                repo = SettingsManager.current().upstream_repo.split('/')
                UPSTREAM_REPO = f"https://github.com/{repo[-2]}/{repo[-1]}"
                await StreamBot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=(
                        f"... ♻️ Restart Successfully...! \n\n"
                        f"Date: {now.strftime('%d/%m/%y')}\n"
                        f"Time: {now.strftime('%I:%M:%S %p')}\n"
                        f"TimeZone: {timezone.zone}\n\n"
                        f"Repo: {UPSTREAM_REPO}\n"
                        f"Branch: {SettingsManager.current().upstream_branch}\n"
                        f"Version: {__version__}"
                    ),
                    parse_mode=enums.ParseMode.HTML
                )
            except Exception as e:
                LOGGER.error(f"Failed to edit restart message: {e}")

            await aioremove(".restartmsg")

    except Exception as e:
        LOGGER.error(f"Error in restart_notification: {e}")


#----- Bot commands
commands = [
    BotCommand("start", "🚀 Start the bot"),
    BotCommand("set", "🎬 Manually add IMDb metadata"),
    BotCommand("stats", "📊 DB and system stats"),
    BotCommand("log", "📄 Send the log file"),
    BotCommand("restart", "♻️ Restart the bot"),
]


async def setup_bot_commands(bot: Client):
    try:
        current_commands = await bot.get_bot_commands()
        if current_commands:
            LOGGER.info(f"Found {len(current_commands)} existing commands. Deleting them...")
            await bot.set_bot_commands([])

        await bot.set_bot_commands(commands)
        LOGGER.info("Bot commands updated successfully.")
    except Exception as e:
        LOGGER.error(f"Error setting up bot commands: {e}")
