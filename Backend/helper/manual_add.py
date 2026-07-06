import re
from typing import Optional, Tuple

from Backend.helper.metadata import parse_media_name
from Backend.helper.pyro import clean_filename, get_readable_file_size, is_media

_PRIVATE_LINK = re.compile(r"t\.me/c/(\d+)(?:/\d+)*/(\d+)")
_PUBLIC_LINK = re.compile(r"t\.me/([A-Za-z][\w]{3,})/(?:\d+/)?(\d+)")


#----- Parse a Telegram post link into (chat_ref, msg_id).
#----- chat_ref is a full -100 int for private channels or a @username string for public ones.
def parse_telegram_link(url: str) -> Tuple[Optional[object], Optional[int]]:
    url = (url or "").strip()
    private = _PRIVATE_LINK.search(url)
    if private:
        return int(f"-100{private.group(1)}"), int(private.group(2))
    public = _PUBLIC_LINK.search(url)
    if public:
        return public.group(1), int(public.group(2))
    return None, None


#----- Map a video pixel height to a standard quality label
def quality_from_height(height: int) -> str:
    if not height:
        return ""
    for threshold, label in ((1800, "2160p"), (1200, "1440p"), (900, "1080p"),
                             (620, "720p"), (400, "480p"), (260, "360p")):
        if height >= threshold:
            return label
    return "240p"


#----- Fetch a message and return the stream fields the manual-add flow needs.
async def resolve_telegram_message(client, url: str = None, chat_id=None, msg_id=None) -> dict:
    if url:
        chat_ref, msg_id = parse_telegram_link(url)
        if chat_ref is None:
            raise ValueError("Could not read that Telegram link. Use a t.me/c/... or t.me/<channel>/... message link.")
    elif chat_id and msg_id:
        chat_ref = int(f"-100{str(chat_id).replace('-100', '')}")
        msg_id = int(msg_id)
    else:
        raise ValueError("Provide a Telegram message link, or a chat id and message id.")

    message = await client.get_messages(chat_ref, msg_id)
    if not message or getattr(message, "empty", False):
        raise ValueError("That message was not found. Make sure the bot is in the channel.")

    media = is_media(message)
    if not media:
        raise ValueError("That message has no downloadable file.")

    #----- Prefer the caption over the raw file name for the display/parse name
    caption = (getattr(message, "caption", None) or "").strip()
    file_name = caption or getattr(media, "file_name", None) or "video"
    raw_size = getattr(media, "file_size", 0) or 0
    parsed = parse_media_name(clean_filename(file_name))

    #----- Real video dimensions beat the filename; documents fall back to the name
    height = getattr(media, "height", 0) or 0
    quality = quality_from_height(height) or parsed.get("quality") or ""

    #----- Original upload date (forward source if forwarded, else this message's date)
    original_date = getattr(message, "forward_date", None) or getattr(message, "date", None)
    upload_year = original_date.year if original_date else 0

    return {
        "chat_id": str(message.chat.id).replace("-100", ""),
        "msg_id": message.id,
        "name": file_name,
        "raw_size": raw_size,
        "size": get_readable_file_size(raw_size),
        "quality": quality,
        "season": parsed.get("season"),
        "episode": parsed.get("episode"),
        "width": getattr(media, "width", 0) or 0,
        "height": height,
        "has_thumb": bool(getattr(media, "thumbs", None)),
        "upload_year": upload_year,
    }
