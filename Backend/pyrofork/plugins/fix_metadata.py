import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from Backend.helper.custom_filter import CustomFilters
from Backend.helper.metadata_manager import metadata_manager

# -------------------------------
# Progress Bar Helper
# -------------------------------
def progress_bar(done, total, length=20):
    filled = int(length * (done / total)) if total else length
    return f"[{'█' * filled}{'░' * (length - filled)}] {done}/{total}"

# -------------------------------
# ETA Helper
# -------------------------------
def format_eta(seconds):
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {sec}s"
    if minutes > 0:
        return f"{minutes}m {sec}s"
    return f"{sec}s"

# -------------------------------
# CANCEL BUTTON HANDLER
# -------------------------------
@Client.on_callback_query(filters.regex("cancel_fix"))
async def cancel_fix(_, query):
    metadata_manager.cancel()
    await query.message.edit_text("❌ Metadata fixing cancellation requested...")
    await query.answer("Cancelling...")

# -------------------------------
# MAIN COMMAND
# -------------------------------
@Client.on_message(filters.command("fixmetadata") & filters.private & CustomFilters.owner, group=10)
async def fix_metadata_handler(_, message):
    if metadata_manager.IS_RUNNING:
        await message.reply_text("⚠️ Metadata fix is already running!")
        return

    status = await message.reply_text(
        "⏳ Initializing metadata fixing...",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_fix")]
        ])
    )

    async def progress_callback(state, done, total, elapsed):
        try:
            if state == "initializing":
                await status.edit_text("⏳ Initializing metadata fixing...")
            elif state == "running":
                 await status.edit_text(
                    f"⏳ Fixing metadata...\n{progress_bar(done, total)}\n⏱ Elapsed: {format_eta(elapsed)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_fix")]
                    ])
                )
            elif state == "cancelled":
                await status.edit_text("❌ Metadata fixing cancelled by user.")
            elif state == "completed":
                await status.edit_text(
                    f"🎉 **Metadata Fix Completed!**\n"
                    f"{progress_bar(done, total)}\n"
                    f"⏱ Time Taken: {format_eta(elapsed)}"
                )
            elif state == "error":
                await status.edit_text("❌ An error occurred during metadata fix.")
        except Exception:
            pass

    success, msg = await metadata_manager.run_fix_metadata(progress_callback=progress_callback)
