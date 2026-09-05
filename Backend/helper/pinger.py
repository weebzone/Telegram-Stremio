import asyncio
import traceback

import aiohttp

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


#----- Periodically self-ping the public status page to keep the instance awake
async def ping():
    sleep_time = 1200

    while True:
        await asyncio.sleep(sleep_time)
        try:
            base = (SettingsManager.current().base_url or "").rstrip("/")
            if not base:
                LOGGER.warning("Ping skipped: base_url is not configured")
                continue

            ping_url = f"{base}/status"

            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(ping_url, allow_redirects=True) as resp:
                    if 200 <= resp.status < 400:
                        LOGGER.info("Pinged keep-alive URL %s — Status: %s", ping_url, resp.status)
                    else:
                        LOGGER.warning(
                            "Keep-alive ping to %s returned %s (check BASE_URL / reverse proxy)",
                            ping_url,
                            resp.status,
                        )
        except asyncio.TimeoutError:
            LOGGER.warning("Timeout: Could not connect to keep-alive URL.")
        except Exception:
            LOGGER.error("Ping failed:\n" + traceback.format_exc())
