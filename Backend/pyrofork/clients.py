from asyncio import gather, create_task
from pyrogram import Client
from Backend.logger import LOGGER
from Backend.config import Telegram
from Backend.pyrofork.bot import multi_clients, work_loads, StreamBot
from os import environ

class TokenParser:
    @staticmethod
    def parse_from_env():
        tokens = {
            c + 1: t
            for c, (_, t) in enumerate(
                filter(
                    lambda n: n[0].startswith("MULTI_TOKEN"), 
                    sorted(environ.items())
                )
            )
        }
        return tokens

async def start_client(client_id, token):
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
        work_loads[client_id] = 0
        return client_id, client
    except Exception as e:
        LOGGER.error(f"Failed to start Client - {client_id} Error: {e}", exc_info=True)
        return None

async def initialize_clients():
    multi_clients[0], work_loads[0] = StreamBot, 0
    all_tokens = TokenParser.parse_from_env()
    if not all_tokens:
        LOGGER.info("No additional Bot Clients found, Using default client")
        return

    tasks = [create_task(start_client(i, token)) for i, token in all_tokens.items()]
    clients = await gather(*tasks)
    clients = {client_id: client for client_id, client in clients if client} 
    multi_clients.update(clients)
    
    if len(multi_clients) != 1:
        LOGGER.info(f"Multi-Client Mode Enabled with {len(multi_clients)} clients")
    else:
        LOGGER.info("No additional clients were initialized, using default client")

