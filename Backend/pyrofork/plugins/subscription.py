from datetime import datetime, timedelta

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from Backend import db
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Configured approvers, falling back to the owner
def _approver_ids() -> list:
    return SettingsManager.current().approver_ids or [Telegram.OWNER_ID]


#----- Resolve a target user's mention and username string for admin captions
async def _resolve_target_info(client: Client, target_user_id: int):
    try:
        target_user = await client.get_users(target_user_id)
        return target_user.mention, (f"@{target_user.username}" if target_user.username else "N/A")
    except Exception:
        return f"User {target_user_id}", "N/A"


#----- Formatted plan/user block shared by approve and reject captions
def _plan_info_text(mention: str, username_str: str, user_id: int, duration, price) -> str:
    return (
        f"👤 <b>User:</b> {mention}\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
        f"🔗 <b>Username:</b> {username_str}\n\n"
        f"📦 <b>Plan:</b> {duration} days (₹{price})"
    )


#----- Update the acting admin's caption plus every other admin's copy
async def _apply_admin_captions(client: Client, callback_query: CallbackQuery, admin_messages: list, status_caption: str):
    await callback_query.message.edit_caption(status_caption)
    acting_msg_id = callback_query.message.id
    for am in admin_messages:
        if am["message_id"] == acting_msg_id:
            continue
        try:
            await client.edit_message_caption(chat_id=am["chat_id"], message_id=am["message_id"], caption=status_caption)
        except Exception:
            pass


#----- Plan button pressed: compute expiry, DM payment instructions, set pending state
@Client.on_callback_query(filters.regex(r"^plan_([a-fA-F0-9]{24})$"))
async def plan_selection(client: Client, callback_query: CallbackQuery):
    if not SettingsManager.current().subscription:
        return await callback_query.answer("Subscriptions are not enabled.", show_alert=True)

    plan_id = callback_query.matches[0].group(1)
    plans = await db.get_subscription_plans()
    plan = next((p for p in plans if p["_id"] == plan_id), None)
    if not plan:
        return await callback_query.answer("Invalid plan.", show_alert=True)

    duration = plan["days"]
    await callback_query.answer()

    user_id = callback_query.from_user.id if callback_query.from_user else callback_query.message.chat.id
    first_name = callback_query.from_user.first_name if callback_query.from_user else callback_query.message.chat.title
    username = callback_query.from_user.username if callback_query.from_user else callback_query.message.chat.username

    await db.update_user_interaction(user_id, first_name, username)

    user = await db.get_user(user_id)
    now = datetime.utcnow()
    current_expiry = user.get("subscription_expiry") if user else None
    if current_expiry and current_expiry > now:
        new_expiry = current_expiry + timedelta(days=int(duration))
    else:
        new_expiry = now + timedelta(days=int(duration))
    expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M UTC")

    settings = SettingsManager.current()
    payment_instructions = settings.payment_instructions
    payment_qr_url = settings.payment_qr_url

    text = (
        f"<b>✅ Plan Selected: {plan['days']} Days</b>\n\n"
        f"<b>💰 Price:</b> ₹{plan['price']}\n"
        f"<b>📅 Expiry (if approved now):</b> {expiry_str}\n\n"
        f"<b>📋 How to Pay:</b>\n"
    )
    text += f"{payment_instructions}\n\n" if payment_instructions else f"Pay ₹{plan['price']} to the admin.\n\n"
    text += (
        "<b>After paying:</b> send your payment screenshot directly here "
        "(in this chat). The admin will review and activate your subscription."
    )

    await db.set_pending_payment(user_id, int(duration), 0, price=plan.get("price", 0))

    #----- Prefer DMing the user so the private screenshot handler can pick it up
    dm_sent = False
    try:
        if payment_qr_url:
            try:
                await client.send_photo(chat_id=user_id, photo=payment_qr_url, caption=f"📷 Scan to pay ₹{plan['price']}")
            except Exception as qe:
                LOGGER.warning(f"Could not send payment QR to {user_id}: {qe}")
        await client.send_message(chat_id=user_id, text=text, reply_markup=ForceReply(selective=True))
        dm_sent = True
    except Exception as e:
        LOGGER.warning(f"Could not DM user {user_id}: {e}")

    if dm_sent:
        await callback_query.answer("✅ Check your DM for payment instructions!", show_alert=True)
    else:
        await callback_query.message.reply_text(
            text + "\n\n⚠️ <i>Please start a DM with the bot first by clicking its username, then send your screenshot there.</i>",
            reply_markup=ForceReply(selective=True),
            quote=True,
        )
        await callback_query.answer()


#----- Forward a user's payment screenshot to all approvers for review
@Client.on_message(filters.photo & filters.private)
async def handle_payment_screenshot(client: Client, message: Message):
    if not SettingsManager.current().subscription:
        return

    sender_id = (message.from_user.id if message.from_user else None) \
        or (message.sender_chat.id if message.sender_chat else None) \
        or message.chat.id

    try:
        LOGGER.debug(f"handle_payment_screenshot triggered by {sender_id}")
        user = await db.get_user(sender_id)
        LOGGER.debug(f"user from DB = {user}")
        if not user or "pending_payment" not in user:
            LOGGER.debug(f"No pending_payment found for {sender_id}")
            await message.reply_text(
                "ℹ️ We received your photo, but you don't have an active payment request.\n\n"
                "Please use /start to select a subscription plan first, then send your payment screenshot.",
                quote=True
            )
            return

        pending = user["pending_payment"]
        duration = pending.get("duration", "?")
        price = pending.get("price", "?")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{sender_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{sender_id}")
        ]])

        user_mention = message.from_user.mention if message.from_user else f"User {sender_id}"
        username_str = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else "N/A"

        admin_text = (
            f"<b>💰 New Payment Screenshot Received</b>\n\n"
            f"<b>👤 User:</b> {user_mention}\n"
            f"<b>🆔 User ID:</b> <code>{sender_id}</code>\n"
            f"<b>🔗 Username:</b> {username_str}\n\n"
            f"<b>📦 Plan Details:</b>\n"
            f"  • Duration: <b>{duration} days</b>\n"
            f"  • Price: <b>₹{price}</b>\n\n"
            f"Please review the screenshot above and approve or reject."
        )

        admin_messages = []
        for approver_id in _approver_ids():
            try:
                sent = await message.copy(approver_id, caption=admin_text, reply_markup=keyboard)
                admin_messages.append({"chat_id": approver_id, "message_id": sent.id})
            except Exception as e:
                LOGGER.error(f"Failed to forward screenshot to approver {approver_id}: {e}")

        await db.set_pending_payment(sender_id, duration, message.id, price=price, admin_messages=admin_messages)

        if admin_messages:
            await message.reply_text(
                "✅ <b>Screenshot Received!</b>\n\n"
                "Your payment screenshot has been forwarded to the admin for review.\n"
                "You will be notified once it's approved. Thank you! 🙏",
                quote=True
            )
        else:
            await message.reply_text(
                "⚠️ Your screenshot was received but we could not reach the admin. "
                "Please contact the admin directly.",
                quote=True
            )

    except Exception as e:
        LOGGER.error(f"Error in handle_payment_screenshot: {e}")
        await message.reply_text(
            f"⚠️ Something went wrong while processing your screenshot. Please try again or contact the admin.\n\nError: {e}",
            quote=True
        )


#----- Approver taps Approve/Reject: update subscription and all admin captions
@Client.on_callback_query(filters.regex(r"^(approve|reject)_(\d+)$"))
async def admin_review(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in _approver_ids():
        return await callback_query.answer("You are not authorized to perform this action.", show_alert=True)

    action = callback_query.matches[0].group(1)
    target_user_id = int(callback_query.matches[0].group(2))
    acting_admin = callback_query.from_user
    admin_name = acting_admin.first_name or acting_admin.username or f"Admin {acting_admin.id}"

    #----- Read admin_messages before any DB write (approve/reject clears pending_payment)
    user_pre = await db.get_user(target_user_id)
    if not user_pre or "pending_payment" not in user_pre:
        return await callback_query.answer("This request has already been processed.", show_alert=True)

    admin_messages = user_pre["pending_payment"].get("admin_messages", [])
    duration = user_pre["pending_payment"].get("duration", "?")
    price = user_pre["pending_payment"].get("price", "?")

    if action == "approve":
        user_data = await db.approve_payment(target_user_id)
        if not user_data:
            return await callback_query.answer("Could not approve — no pending payment found.", show_alert=True)

        try:
            user_obj = await db.get_user(target_user_id)
            user_name = (user_obj.get("first_name") or user_obj.get("username") or str(target_user_id)) if user_obj else str(target_user_id)
            token_doc = await db.add_api_token(name=user_name, user_id=target_user_id)
            addon_url = f"{SettingsManager.current().base_url}/stremio/{token_doc.get('token')}/manifest.json"
        except Exception:
            addon_url = None

        try:
            invite_link = await client.create_chat_invite_link(
                chat_id=SettingsManager.current().subscription_group_id,
                member_limit=1,
                expire_date=datetime.utcnow() + timedelta(days=1)
            )
            invite_text = f"\n\n🔗 <b>Group Invite:</b> {invite_link.invite_link}"
        except Exception:
            invite_text = ""

        expiry_str = user_data["subscription_expiry"].strftime("%Y-%m-%d")
        success_text = (
            f"🎉 <b>Payment Approved!</b>\n\n"
            f"Your subscription is now active until <b>{expiry_str}</b>."
            f"{invite_text}"
        )
        if addon_url:
            success_text += (
                f"\n\n🎬 <b>Stremio Addon — Install Link:</b>\n"
                f"<code>{addon_url}</code>\n\n"
                f"Tap the link above → <b>Install</b> in Stremio to start watching!"
            )
        await client.send_message(target_user_id, success_text)

        mention, username_str = await _resolve_target_info(client, target_user_id)
        info_text = _plan_info_text(mention, username_str, target_user_id, duration, price)
        await _apply_admin_captions(client, callback_query, admin_messages, f"✅ <b>Approved by {admin_name}</b>\n\n{info_text}")

    elif action == "reject":
        if not await db.reject_payment(target_user_id):
            return await callback_query.answer("Could not reject — no pending payment found.", show_alert=True)

        await client.send_message(
            target_user_id,
            "❌ <b>Payment Rejected</b>\n\nYour recent payment submission was rejected by the admin. Please contact the admin or try submitting again."
        )
        mention, username_str = await _resolve_target_info(client, target_user_id)
        info_text = _plan_info_text(mention, username_str, target_user_id, duration, price)
        await _apply_admin_captions(client, callback_query, admin_messages, f"❌ <b>Rejected by {admin_name}</b>\n\n{info_text}")


#----- /status: report the caller's active subscription and time remaining
@Client.on_message(filters.command("status"))
async def check_status(client: Client, message: Message):
    if not SettingsManager.current().subscription:
        return

    user_id = (message.from_user.id if message.from_user else None) or (message.sender_chat.id if message.sender_chat else None) or message.chat.id

    user = await db.get_user(user_id)
    if not user or user.get("subscription_status") != "active":
        return await message.reply_text("You do not have an active subscription.")

    expiry = user.get("subscription_expiry")
    if not expiry:
        return await message.reply_text("Error retrieving expiry date.")

    now = datetime.utcnow()
    if now > expiry:
        return await message.reply_text("Your subscription has expired.")

    remaining = expiry - now
    days = remaining.days
    hours = remaining.seconds // 3600
    await message.reply_text(
        f"<b>Subscription Status:</b> Active ✅\n"
        f"<b>Expiry Date:</b> {expiry.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"<b>Time Remaining:</b> {days} days and {hours} hours"
    )
