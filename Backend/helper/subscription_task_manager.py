import asyncio
from typing import Optional

from pyrogram import Client

from Backend import db
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import get_streambot_url

_task: Optional[asyncio.Task] = None


#----- Periodically kick expired subscribers and remind those expiring soon
async def subscription_checker_loop(bot: Client):
    while True:
        try:
            if not SettingsManager.current().subscription:
                await asyncio.sleep(60)
                continue

            LOGGER.info("Running subscription checker...")

            #----- Kick expired users (ban+unban) and notify them
            expired_users = await db.get_expired_users()
            for user in expired_users:
                user_id = user["_id"]
                try:
                    await bot.ban_chat_member(SettingsManager.current().subscription_group_id, user_id)
                    await bot.unban_chat_member(SettingsManager.current().subscription_group_id, user_id)
                    await db.mark_user_expired(user_id)
                    await bot.send_message(
                        user_id,
                        "❌ <b>Subscription Expired</b>\n\n"
                        "Your subscription has expired, and you have been removed from the private group.\n"
                        f"Please open the bot {get_streambot_url()} and send /start to renew your subscription and regain access."
                    )
                    LOGGER.info(f"Kicked expired user {user_id}")
                except Exception as e:
                    LOGGER.error(f"Failed to kick/notify expired user {user_id}: {e}")

            #----- Remind users expiring within 24 hours
            expiring_users = await db.get_expiring_users(hours=24)
            for user in expiring_users:
                user_id = user["_id"]
                expiry = user["subscription_expiry"]
                try:
                    await bot.send_message(
                        user_id,
                        f"⚠️ <b>Subscription Expiring Soon</b>\n\n"
                        f"Your subscription will expire on <b>{expiry.strftime('%Y-%m-%d %H:%M UTC')}</b>.\n"
                        f"Please open the bot {get_streambot_url()} and send /start to renew your plan before you lose access to the group!"
                    )
                    await db.mark_reminder_sent(user_id)
                    LOGGER.info(f"Sent expiry reminder to user {user_id}")
                except Exception as e:
                    LOGGER.error(f"Failed to send reminder to user {user_id}: {e}")

            await asyncio.sleep(3600)

        except asyncio.CancelledError:
            LOGGER.info("Subscription checker loop cancelled.")
            raise
        except Exception as e:
            LOGGER.error(f"Error in subscription checker loop: {e}")
            await asyncio.sleep(300)


#----- Whether the checker task is currently running
def is_running() -> bool:
    return _task is not None and not _task.done()


#----- Start the subscription checker background task
async def start(bot) -> bool:
    global _task
    if is_running():
        return False
    _task = asyncio.create_task(subscription_checker_loop(bot))
    LOGGER.info("Subscription Checker Task Started.")
    return True


#----- Cancel the subscription checker background task
async def stop() -> bool:
    global _task
    if not is_running():
        _task = None
        return False

    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        LOGGER.warning(f"Error while stopping subscription checker task: {e}")
    finally:
        _task = None

    LOGGER.info("Subscription Checker Task Stopped.")
    return True


#----- Start the checker only when subscriptions are enabled
async def sync(bot) -> None:
    if SettingsManager.current().subscription:
        await start(bot)
