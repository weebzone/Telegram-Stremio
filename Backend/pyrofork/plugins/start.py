from datetime import datetime

from pyrogram import Client, enums, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from Backend import db
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- /start: hand out the Stremio addon link, gated by subscription state
@Client.on_message(filters.command('start') & filters.private, group=10)
async def send_start_message(client: Client, message: Message):
    try:
        user_id = (message.from_user.id if message.from_user else None) or (message.sender_chat.id if message.sender_chat else None) or message.chat.id
        base_url = SettingsManager.current().base_url
        addon_url = f"{base_url}/stremio/manifest.json"

        #----- No subscription mode: owner-only, single personal token
        if not SettingsManager.current().subscription:
            if user_id != Telegram.OWNER_ID:
                return
            user_name = (message.from_user.first_name or message.from_user.username or f"User {user_id}") if message.from_user else f"Chat {user_id}"
            try:
                token_doc = await db.add_api_token(name=user_name, user_id=user_id)
                addon_url = f"{base_url}/stremio/{token_doc.get('token')}/manifest.json"
            except Exception as e:
                LOGGER.error(f"Error ensuring token for free user: {e}")

            await message.reply_text(
                '🎉 <b>Welcome to the Telegram Stremio Media Server!</b>\n\n'
                'Here is your personal Stremio Addon link:\n\n'
                '🎬 <b>Stremio Addon — Install Link:</b>\n'
                f'<code>{addon_url}</code>\n\n'
                'Tap the link above → <b>Install</b> in Stremio to start watching!',
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )
            return

        #----- Subscription mode: verify active subscription, else offer plans
        user = await db.get_user(user_id)
        now = datetime.utcnow()

        is_active = db.is_subscription_active(user, now)
        if not is_active and user and user.get("subscription_status") == "active":
            await db.mark_user_expired(user_id)

        if not is_active:
            plans = await db.get_subscription_plans()
            if not plans:
                return await message.reply_text(
                    '<b>Welcome to the Telegram Stremio Private Group!</b>\n\n'
                    'Currently, no subscription plans are set up. Please contact the administrator.',
                    quote=True,
                    parse_mode=enums.ParseMode.HTML
                )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{plan['days']} Days - ₹{plan['price']}", callback_data=f"plan_{plan['_id']}")]
                for plan in plans
            ])
            return await message.reply_text(
                '<b>Welcome to the Telegram Stremio Private Group!</b>\n\n'
                'Access to this bot and the Stremio Addon requires an active subscription.\n'
                'Please select a subscription plan below to continue:',
                reply_markup=keyboard,
                quote=True,
                parse_mode=enums.ParseMode.HTML
            )

        #----- Active subscriber: return their existing token link
        all_tokens = await db.get_all_api_tokens()
        token_doc = next((t for t in all_tokens if t.get("user_id") == user_id), None)
        if token_doc and "token" in token_doc:
            addon_url = f"{base_url}/stremio/{token_doc['token']}/manifest.json"

        await message.reply_text(
            '🎉 <b>Welcome back to the Telegram Stremio Subscription Manager!</b>\n\n'
            'Your subscription is active. Here is your personal addon link:\n\n'
            '🎬 <b>Stremio Addon — Install Link:</b>\n'
            f'<code>{addon_url}</code>\n\n'
            'Tap the link above → <b>Install</b> in Stremio to start watching!',
            quote=True,
            parse_mode=enums.ParseMode.HTML
        )

    except Exception as e:
        await message.reply_text(f"⚠️ Error: {e}")
        LOGGER.error(f"Error in /start handler: {e}")
