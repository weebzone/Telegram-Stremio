import asyncio

from pyrogram.errors import FloodWait

from Backend.helper.encrypt import decode_string
from Backend.helper.pyro import is_media
from Backend.logger import LOGGER
from Backend.pyrofork.bot import multi_clients

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"


#----- Background task that periodically flags dead Telegram links
class DeadLinkChecker:
    def __init__(self, db, app, check_interval_hours: int = 24):
        self.db = db
        self.app = app
        self.check_interval_seconds = check_interval_hours * 3600
        self.is_running = False

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        LOGGER.info(f"Started Dead Link Checker background task (Interval: {self.check_interval_seconds}s)")
        asyncio.create_task(self._run_loop())

    async def _run_loop(self):
        await asyncio.sleep(120)

        while self.is_running:
            try:
                LOGGER.info("Starting Dead Link Checker scan...")
                await self._scan_all_media()
                LOGGER.info("Dead Link Checker scan complete.")
            except Exception as e:
                LOGGER.error(f"Error in Dead Link Checker loop: {e}")

            await asyncio.sleep(self.check_interval_seconds)

    async def _scan_all_media(self):
        if not multi_clients:
            LOGGER.warning("No bot clients available for Dead Link Checker.")
            return

        clients = list(multi_clients.values())

        for i in range(1, self.db.current_db_index + 1):
            active_db = self.db.dbs[f"storage_{i}"]

            #----- Movies (snapshot ids first so no cursor stays open during the slow Telegram checks)
            try:
                movie_ids = [d["_id"] for d in await active_db["movie"].find(
                    {"telegram": {"$exists": True, "$not": {"$size": 0}}, "telegram.is_dead": {"$ne": True}},
                    {"_id": 1},
                ).to_list(None)]
                for movie_id in movie_ids:
                    movie = await active_db["movie"].find_one({"_id": movie_id})
                    if not movie:
                        continue
                    tmdb_id = movie.get("tmdb_id")
                    for quality in movie.get("telegram", []):
                        if quality.get("is_dead"):
                            continue
                        status = await self._check_file_alive(clients, quality.get("id"))
                        if status == DEAD:
                            LOGGER.warning(f"Found dead link for Movie {tmdb_id} (Quality: {quality.get('quality')})")
                            await self.db.flag_dead_link("movie", tmdb_id, i, quality.get("id"))
                        await asyncio.sleep(0.5)
            except Exception as e:
                LOGGER.error(f"Error scanning movies in DB {i}: {e}")

            #----- TV Shows (snapshot ids first, then re-read each doc with a short query)
            try:
                tv_ids = [d["_id"] for d in await active_db["tv"].find(
                    {"seasons.episodes.telegram": {"$exists": True, "$not": {"$size": 0}}, "seasons.episodes.telegram.is_dead": {"$ne": True}},
                    {"_id": 1},
                ).to_list(None)]
                for tv_id in tv_ids:
                    tv = await active_db["tv"].find_one({"_id": tv_id})
                    if not tv:
                        continue
                    tmdb_id = tv.get("tmdb_id")
                    for season in tv.get("seasons", []):
                        for ep in season.get("episodes", []):
                            for quality in ep.get("telegram", []):
                                if quality.get("is_dead"):
                                    continue
                                status = await self._check_file_alive(clients, quality.get("id"))
                                if status == DEAD:
                                    LOGGER.warning(f"Found dead link for TV {tmdb_id} S{season.get('season_number')}E{ep.get('episode_number')} (Quality: {quality.get('quality')})")
                                    await self.db.flag_dead_link("tv", tmdb_id, i, quality.get("id"))
                                await asyncio.sleep(0.5)
            except Exception as e:
                LOGGER.error(f"Error scanning TV shows in DB {i}: {e}")

    async def _check_file_alive(self, clients, quality_id: str) -> str:
        try:
            decoded = await decode_string(quality_id)
        except Exception as e:
            LOGGER.error(f"Link checker failed to decode {quality_id}: {e}")
            return UNKNOWN
        if not decoded:
            return UNKNOWN

        #----- Split file: every part must be alive; one confirmed-dead part kills the link
        if "parts" in decoded:
            parts = decoded.get("parts") or []
            if not parts:
                return UNKNOWN
            saw_unknown = False
            for part in parts:
                status = await self._check_message_status(clients, part.get("chat_id"), part.get("msg_id"))
                if status == DEAD:
                    return DEAD
                if status == UNKNOWN:
                    saw_unknown = True
            return UNKNOWN if saw_unknown else ALIVE

        #----- Single file
        if "chat_id" not in decoded or "msg_id" not in decoded:
            return UNKNOWN
        return await self._check_message_status(clients, decoded["chat_id"], decoded["msg_id"])

    async def _check_message_status(self, clients, chat_id, msg_id) -> str:
        if chat_id is None or msg_id is None:
            return UNKNOWN

        chat_id = int(f"-100{chat_id}")
        msg_id = int(msg_id)
        confirmed_dead = False
        for client in clients:
            try:
                message = await client.get_messages(chat_id, msg_id)
            except FloodWait as e:
                await asyncio.sleep(getattr(e, "value", 5) + 1)
                continue
            except Exception:
                continue

            if message is None or message.empty:
                confirmed_dead = True
                continue
            if is_media(message):
                return ALIVE
            confirmed_dead = True

        return DEAD if confirmed_dead else UNKNOWN
