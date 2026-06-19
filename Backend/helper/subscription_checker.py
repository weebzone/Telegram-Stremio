import asyncio
from pyrogram import Client
from Backend.helper.settings_manager import SettingsManager
from Backend import db
from datetime import datetime
from Backend.logger import LOGGER


config = SettingsManager.current()
async def subscription_checker_loop(bot: Client):
    while True:
        try:
            if not config.subscription:
                await asyncio.sleep(3600)
                continue

            LOGGER.info("Running subscription checker...")

            # 1. Fetch expired users & kick them
            expired_users = await db.get_expired_users()
            for user in expired_users:
                user_id = user["_id"]
                try:
                    # Ban then unban to kick the user without permanently banning them
                    await bot.ban_chat_member(config.subscription_group_id, user_id)
                    await bot.unban_chat_member(config.subscription_group_id, user_id)
                    
                    await db.mark_user_expired(user_id)
                    
                    # Notify user
                    await bot.send_message(
                        user_id,
                        "❌ <b>Subscription Expired</b>\n\n"
                        "Your subscription has expired, and you have been removed from the private group.\n"
                        f"Please go to {config.subscription_url} and send /start to renew your subscription and regain access."
                    )
                    LOGGER.info(f"Kicked expired user {user_id}")
                except Exception as e:
                    LOGGER.error(f"Failed to kick/notify expired user {user_id}: {e}")

            # 2. Remind users expiring in 24 hours
            expiring_users = await db.get_expiring_users(hours=24)
            for user in expiring_users:
                user_id = user["_id"]
                expiry = user["subscription_expiry"]
                try:
                    await bot.send_message(
                        user_id,
                        f"⚠️ <b>Subscription Expiring Soon</b>\n\n"
                        f"Your subscription will expire on <b>{expiry.strftime('%Y-%m-%d %H:%M UTC')}</b>.\n"
                        f"Please go to {config.subscription_url} and send /start to renew your plan before you lose access to the group!"
                    )
                    await db.mark_reminder_sent(user_id)
                    LOGGER.info(f"Sent expiry reminder to user {user_id}")
                except Exception as e:
                    LOGGER.error(f"Failed to send reminder to user {user_id}: {e}")

            # Check every hour
            await asyncio.sleep(3600)

        except Exception as e:
            LOGGER.error(f"Error in subscription checker loop: {e}")
            await asyncio.sleep(300) # Wait 5 minutes before retrying on error
