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


#----- Connect backends AFTER the web server is already listening.
#----- Hugging Face Spaces requires the HTTP port open within ~60 s,
#----- so we start the web server first and connect DB/Telegram in background.
async def _connect_backends():
    try:
        LOGGER.info("Connecting to MongoDB...")
        await asyncio.wait_for(db.connect(), timeout=30)
        LOGGER.info("MongoDB connected.")

        await SettingsManager.initialize(db)
        app.add_middleware(SessionMiddleware, secret_key=SettingsManager.current().session_secret or secrets.token_hex(32))

        await scan_manager.load(db)
        dbcheck_manager.bind_db(db)
        duplicate_manager.bind_db(db)
        await db.reload_extra_databases(SettingsManager.current().extra_databases)

        LOGGER.info("Starting StreamBot...")
        await asyncio.wait_for(StreamBot.start(), timeout=30)
        StreamBot.username = StreamBot.me.username
        LOGGER.info(f"Bot Client : [@{StreamBot.username}]")

        if Userbot is not None:
            LOGGER.info("Starting Userbot...")
            await asyncio.wait_for(Userbot.start(), timeout=30)
            Userbot.username = Userbot.me.username
            LOGGER.info(f"Userbot Client : [@{Userbot.username}]")
        else:
            LOGGER.info("Userbot not configured (USER_SESSION_STRING empty) — running with StreamBot only.")

        LOGGER.info("Initializing Multi Clients...")
        await asyncio.wait_for(initialize_clients(), timeout=60)

        await setup_bot_commands(StreamBot)
        await restart_notification()

        loop.create_task(ping())
        link_checker_task = DeadLinkChecker(db, app, check_interval_hours=24)
        loop.create_task(link_checker_task.start())

        await subscription_task_manager.sync(StreamBot)
        LOGGER.info("All backends connected — Telegram-Stremio is fully ready!")

    except asyncio.TimeoutError as e:
        LOGGER.error(f"Backend connection timed out: {e}")
    except Exception:
        LOGGER.error("Error during backend connection:\n" + format_exc())


#----- Boot: (1) start web server immediately, (2) connect backends in background, (3) idle
async def start_services():
    try:
        LOGGER.info(f"Initializing Telegram-Stremio v-{__version__}")

        LOGGER.info("Starting Web Server (HTTP)...")
        loop.create_task(server.serve())
        await asyncio.sleep(1)

        loop.create_task(_connect_backends())

        LOGGER.info("Telegram-Stremio Web Server is live — backends connecting in background.")
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
