from asyncio import create_task, gather

from pyrogram import Client

from Backend.config import Telegram
from Backend.fastapi.routes.stream_routes import _streamer_by_client
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, client_dc_map, multi_clients, work_loads

client_tokens: dict[int, str] = {}


#----- Read the configured multi-client tokens from settings
class TokenParser:
    @staticmethod
    def parse_from_settings() -> dict[int, str]:
        tokens = SettingsManager.current().multi_tokens
        return {i + 1: tok.strip() for i, tok in enumerate(tokens) if tok and tok.strip()}


#----- Start a single bot client and register its DC/workload
async def start_client(client_id: int, token: str):
    try:
        LOGGER.info(f"Starting - Bot Client {client_id}")
        client = await Client(
            name=str(client_id),
            api_id=Telegram.API_ID,
            api_hash=Telegram.API_HASH,
            bot_token=token,
            sleep_threshold=100,
            no_updates=True,
            in_memory=True
        ).start()

        try:
            client_dc = await client.storage.dc_id()
            client_dc_map[client_id] = client_dc
            LOGGER.info(f"Client {client_id} connected to DC {client_dc}")
        except Exception as e:
            LOGGER.warning(f"Could not get DC for Client {client_id}: {e}")
            client_dc_map[client_id] = None

        work_loads[client_id] = 0
        return client_id, client
    except Exception as e:
        LOGGER.error(f"Failed to start Client - {client_id} Error: {e}", exc_info=True)
        return None


#----- Stop a client and purge all of its registry entries
async def stop_client(client_id: int) -> None:
    client = multi_clients.pop(client_id, None)
    work_loads.pop(client_id, None)
    client_dc_map.pop(client_id, None)
    client_tokens.pop(client_id, None)
    _streamer_by_client.pop(client_id, None)

    if client:
        try:
            await client.stop()
            LOGGER.info(f"Stopped Bot Client {client_id}")
        except Exception as e:
            LOGGER.warning(f"Error stopping Client {client_id}: {e}")


#----- Bring up the main client plus every configured extra client
async def initialize_clients() -> None:
    multi_clients[0], work_loads[0] = StreamBot, 0

    try:
        main_dc = await StreamBot.storage.dc_id()
        client_dc_map[0] = main_dc
        LOGGER.info(f"Main StreamBot connected to DC {main_dc}")
    except Exception as e:
        LOGGER.warning(f"Could not get DC for StreamBot: {e}")
        client_dc_map[0] = None

    all_tokens = TokenParser.parse_from_settings()
    if not all_tokens:
        LOGGER.info("No additional Bot Clients found, Using default client")
        return

    tasks = [create_task(start_client(i, token)) for i, token in all_tokens.items()]
    results = await gather(*tasks)

    started = {client_id: client for client_id, client in results if client}
    multi_clients.update(started)
    for client_id, token in all_tokens.items():
        if client_id in started:
            client_tokens[client_id] = token

    if len(multi_clients) != 1:
        LOGGER.info(f"Multi-Client Mode Enabled with {len(multi_clients)} clients")
    else:
        LOGGER.info("No additional clients were initialized, using default client")


#----- Reconcile running clients with the current token settings
async def reload_multi_token_clients() -> dict:
    new_tokens = TokenParser.parse_from_settings()

    old_ids = set(client_tokens.keys())
    new_ids = set(new_tokens.keys())

    to_stop = [cid for cid in old_ids if cid not in new_ids or client_tokens.get(cid) != new_tokens.get(cid)]
    for cid in to_stop:
        await stop_client(cid)

    to_start = {cid: tok for cid, tok in new_tokens.items() if cid not in old_ids or client_tokens.get(cid) != tok}

    if to_start:
        tasks = [create_task(start_client(cid, tok)) for cid, tok in to_start.items()]
        results = await gather(*tasks)
        started = {cid: client for cid, client in results if client}
        multi_clients.update(started)
        for cid, tok in to_start.items():
            if cid in started:
                client_tokens[cid] = tok

    LOGGER.info(
        f"Multi-token reload complete — {len(to_stop)} stopped, "
        f"{len(to_start)} (re)started, {len(multi_clients)} total clients active."
    )
    return {
        "stopped": len(to_stop),
        "started": len(to_start),
        "total_clients": len(multi_clients),
    }
