from asyncio import get_event_loop, sleep as asleep
import asyncio
import logging
from traceback import format_exc
from pyrogram import idle
from Backend import __version__, db
from Backend.helper.pinger import ping
from Backend.logger import LOGGER
from Backend.fastapi import server
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.pyro import restart_notification, setup_bot_commands
from Backend.pyrofork.bot import Userbot, StreamBot
from Backend.pyrofork.clients import initialize_clients
from Backend.helper import subscription_task_manager
from Backend.helper.scan_manager import scan_manager, dbcheck_manager
from Backend.helper.link_checker import DeadLinkChecker
from Backend.fastapi.main import app
from Backend.helper.auto_catalog import start_auto_catalog_sync_background


loop = get_event_loop()

async def start_services():
    try:
        LOGGER.info(f"Initializing Telegram-Stremio v-{__version__}")
        await asleep(1.2)
        
        await db.connect()
        await asleep(1.2)

        await SettingsManager.initialize(db)
        await asleep(0.5)
        
        try:
            await scan_manager.load(db)
            dbcheck_manager.bind_db(db)
        except Exception as e:
            LOGGER.error(f"Failed to restore scan manager state on startup: {e}")
        await asleep(0.3)

        try:
            await db.reload_extra_databases(SettingsManager.current().extra_databases)
        except Exception as e:
            LOGGER.error(f"Failed to reconnect extra storage databases on startup: {e}")
        await asleep(0.5)

        await StreamBot.start()
        StreamBot.username = StreamBot.me.username
        LOGGER.info(f"Bot Client : [@{StreamBot.username}]")
        await asleep(1.2)

        if Userbot is not None:
            await Userbot.start()
            Userbot.username = Userbot.me.username
            LOGGER.info(f"Userbot Client : [@{Userbot.username}] (Global Search / fallback enabled)")
        else:
            LOGGER.info("Userbot not configured (USER_SESSION_STRING empty) — running with StreamBot only.")
        await asleep(1.2)

        LOGGER.info("Initializing Multi Clients...")
        await initialize_clients()
        await asleep(2)
        
        await setup_bot_commands(StreamBot)
        await asleep(2)

        LOGGER.info('Initializing Telegram-Stremio Web Server...')
        await restart_notification()
        loop.create_task(server.serve())
        loop.create_task(ping())
        
        link_checker_task = DeadLinkChecker(db, app, check_interval_hours=24)
        loop.create_task(link_checker_task.start())

        
        loop.create_task(start_auto_catalog_sync_background(db, delay_seconds=20, full_rebuild=False))

        await subscription_task_manager.sync(StreamBot)
        
        LOGGER.info("Telegram-Stremio Started Successfully!")
        await idle()
    except Exception:
        LOGGER.error("Error during startup:\n" + format_exc())

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
