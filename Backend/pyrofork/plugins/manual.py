from pyrogram import Client, enums, filters
from pyrogram.types import Message

import Backend
from Backend import db
from Backend.helper.custom_filter import CustomFilters
from Backend.logger import LOGGER


def _parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


#----- Start a custom-id session: /set <custom_id> [season] [episode]
async def _set_custom_session(message: Message, args: list) -> None:
    custom_id = _parse_int(args[0])
    season = _parse_int(args[1]) if len(args) >= 2 else None
    episode = _parse_int(args[2]) if len(args) >= 3 else None

    movie_loc = await db.find_media_doc("movie", custom_id)
    tv_loc = await db.find_media_doc("tv", custom_id)

    if season is not None:
        if not tv_loc:
            await message.reply_text(f"⚠️ No TV show found with custom id <code>{custom_id}</code>.",
                                     quote=True, parse_mode=enums.ParseMode.HTML)
            return
        if episode is not None and _parse_int(args[2]) is None:
            await message.reply_text("⚠️ Episode number must be a valid number.", quote=True)
            return
        Backend.MANUAL_SESSION = {"custom_id": custom_id, "media_type": "tv", "season": season, "episode": episode}
        Backend.USE_DEFAULT_ID = None
        title = tv_loc[0].get("title") or "Unknown"
        mode = (f"Every file → <b>S{season:02d}E{episode:02d}</b> (multiple qualities of one episode)."
                if episode is not None
                else f"Each file → <b>next episode</b> of Season {season} (E1, E2, E3…).")
        await message.reply_text(
            f"✅ <b>Custom session started</b>\n\n"
            f"📺 <b>{title}</b> · Season {season}\n"
            f"{mode}\n\n"
            f"Forward the files to your manual channel now.\n"
            f"Send <code>/set</code> to end the session.",
            quote=True, parse_mode=enums.ParseMode.HTML
        )
        return

    #----- No season provided -> movie session
    if not movie_loc:
        if tv_loc:
            await message.reply_text("⚠️ That is a TV show — a season number is required: "
                                     "<code>/set &lt;custom_id&gt; &lt;season&gt; [episode]</code>.",
                                     quote=True, parse_mode=enums.ParseMode.HTML)
        else:
            await message.reply_text(f"⚠️ No media found with custom id <code>{custom_id}</code>.",
                                     quote=True, parse_mode=enums.ParseMode.HTML)
        return

    Backend.MANUAL_SESSION = {"custom_id": custom_id, "media_type": "movie", "season": None, "episode": None}
    Backend.USE_DEFAULT_ID = None
    title = movie_loc[0].get("title") or "Unknown"
    await message.reply_text(
        f"✅ <b>Custom session started</b>\n\n"
        f"🎬 <b>{title}</b>\n"
        f"Every file forwarded to your manual channel is added as a quality of this movie.\n\n"
        f"Send <code>/set</code> to end the session.",
        quote=True, parse_mode=enums.ParseMode.HTML
    )


#----- Owner-only /set: manage the default IMDB/TMDB URL or a custom-id session
@Client.on_message(filters.command('set') & filters.private & CustomFilters.owner, group=10)
async def manual(client: Client, message: Message):
    try:
        args = message.text.split()

        #----- No argument -> clear everything
        if len(args) == 1:
            Backend.USE_DEFAULT_ID = None
            Backend.MANUAL_SESSION = None
            await message.reply_text(
                "✅ <b>Session cleared!</b>\n\n"
                "No default URL or custom session is active. Uploads are no longer auto-linked.",
                quote=True, parse_mode=enums.ParseMode.HTML
            )
            return

        #----- Negative first token -> custom-id session (personal files)
        first_int = _parse_int(args[1])
        if first_int is not None and first_int < 0:
            await _set_custom_session(message, args[1:])
            return

        #----- Otherwise -> default IMDB/TMDB URL flow (works in auth & manual channels)
        Backend.USE_DEFAULT_ID = message.text.split(maxsplit=1)[1].strip()
        Backend.MANUAL_SESSION = None
        await message.reply_text(
            f"✅ <b>Default IMDB/TMDB URL Set!</b>\n\n"
            f"Now the bot will use this URL for any files you send:\n"
            f"<code>{Backend.USE_DEFAULT_ID}</code>\n\n"
            f"<b>Instructions:</b>\n"
            f"1. Forward the related movie or TV show files to your channel.\n"
            f"2. Once all files are uploaded, clear the default URL by sending <code>/set</code> without any URL.",
            quote=True,
            parse_mode=enums.ParseMode.HTML
        )

    except Exception as e:
        LOGGER.error(f"Error in /set handler: {e}")
        await message.reply_text(f"⚠️ An error occurred: {e}")
