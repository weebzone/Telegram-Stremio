from __future__ import annotations

from Backend.helper.telegram.pyro import get_scan_client
from Backend.helper.settings.manager import SettingsManager
from Backend.logger import LOGGER


async def list_auth_channels() -> dict:
    channels = list(SettingsManager.current().auth_channels)
    client = get_scan_client()
    result = []
    for ch in channels:
        name = str(ch)
        try:
            if client is not None:
                chat = await client.get_chat(int(ch) if str(ch).lstrip("-").isdigit() else ch)
                name = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(ch)
        except Exception as e:
            LOGGER.warning(f"[Tools] Could not resolve channel {ch}: {e}")
        result.append({"id": str(ch), "name": name})
    return {"status": "success", "data": result}
