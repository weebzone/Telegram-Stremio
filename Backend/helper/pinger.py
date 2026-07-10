import asyncio
import traceback

import aiohttp

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Periodically self-ping the stats endpoint to keep the instance awake
async def ping():
    sleep_time = 1200
    manifest_url = f"{SettingsManager.current().base_url}/api/system/stats"

    while True:
        await asyncio.sleep(sleep_time)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(manifest_url) as resp:
                    LOGGER.info(f"Pinged manifest URL — Status: {resp.status}")
        except asyncio.TimeoutError:
            LOGGER.warning("Timeout: Could not connect to manifest URL.")
        except Exception:
            LOGGER.error("Ping failed:\n" + traceback.format_exc())
