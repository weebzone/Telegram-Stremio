import asyncio
import logging
import secrets
from traceback import format_exc

from pyrogram import idle
from starlette.middleware.sessions import SessionMiddleware

from Backend import __version__, db
from Backend.fastapi import server
from Backend.fastapi.main import app
from Backend.helper import subscription_task_manager
from Backend.helper.link_checker import DeadLinkChecker
from Backend.helper.pinger import ping
from Backend.helper.pyro import restart_notification, setup_bot_commands
from Backend.helper.scan_manager import dbcheck_manager, duplicate_manager, scan_manager
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, Userbot
from Backend.pyrofork.clients import initialize_clients

loop = asyncio.get_event_loop()


#----- Boot every subsystem then idle the bot
async def start_services():
    try:
        LOGGER.info(f"Initializing Telegram-Stremio v-{__version__}")
        await asyncio.sleep(1.2)

        await db.connect()
        await asyncio.sleep(1.2)

        await SettingsManager.initialize(db)
        app.add_middleware(SessionMiddleware, secret_key=SettingsManager.current().session_secret or secrets.token_hex(32))
        await asyncio.sleep(0.5)

        await scan_manager.load(db)
        dbcheck_manager.bind_db(db)
        duplicate_manager.bind_db(db)
        await asyncio.sleep(0.3)

        await db.reload_extra_databases(SettingsManager.current().extra_databases)
        await asyncio.sleep(0.5)

        await StreamBot.start()
        StreamBot.username = StreamBot.me.username
        LOGGER.info(f"Bot Client : [@{StreamBot.username}]")
        await asyncio.sleep(1.2)

        if Userbot is not None:
            await Userbot.start()
            Userbot.username = Userbot.me.username
            LOGGER.info(f"Userbot Client : [@{Userbot.username}]")
        else:
            LOGGER.info("Userbot not configured (USER_SESSION_STRING empty) — running with StreamBot only.")
        await asyncio.sleep(1.2)

        LOGGER.info("Initializing Multi Clients...")
        await initialize_clients()
        await asyncio.sleep(2)

        await setup_bot_commands(StreamBot)
        await asyncio.sleep(2)

        LOGGER.info('Initializing Telegram-Stremio Web Server...')
        await restart_notification()
        loop.create_task(server.serve())
        loop.create_task(ping())

        link_checker_task = DeadLinkChecker(db, app, check_interval_hours=24)
        loop.create_task(link_checker_task.start())

        await subscription_task_manager.sync(StreamBot)

        LOGGER.info("Telegram-Stremio Started Successfully!")
        await idle()
    except Exception:
        LOGGER.error("Error during startup:\n" + format_exc())


#----- Cancel pending tasks and shut clients down
async def stop_services():
    try:
        LOGGER.info("Stopping services...")

        pending_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)

        await StreamBot.stop()
        if Userbot is not None:
            await Userbot.stop()

        await db.disconnect()
        LOGGER.info("Services stopped successfully.")
    except Exception:
        LOGGER.error("Error during shutdown:\n" + format_exc())


if __name__ == '__main__':
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        LOGGER.info('Service Stopping...')
    except Exception:
        LOGGER.error(format_exc())
    finally:
        loop.run_until_complete(stop_services())
        loop.stop()
        logging.shutdown()
