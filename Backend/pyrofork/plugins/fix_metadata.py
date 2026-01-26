import time
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from Backend import db
from Backend.helper.custom_filter import CustomFilters
from Backend.helper.metadata import fetch_tv_metadata, fetch_movie_metadata
from Backend.logger import LOGGER

CANCEL_REQUESTED = False

# -------------------------------
# Progress Bar Helper
# -------------------------------
def progress_bar(done, total, length=20):
    filled = int(length * (done / total)) if total else length
    return f"[{'█' * filled}{'░' * (length - filled)}] {done}/{total}"

# -------------------------------
# ETA Helper
# -------------------------------
def format_eta(seconds):
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {sec}s"
    if minutes > 0:
        return f"{minutes}m {sec}s"
    return f"{sec}s"

# -------------------------------
# CANCEL BUTTON HANDLER
# -------------------------------
@Client.on_callback_query(filters.regex("cancel_fix"))
async def cancel_fix(_, query):
    global CANCEL_REQUESTED
    CANCEL_REQUESTED = True
    await query.message.edit_text("❌ Metadata fixing has been cancelled by the user.")
    await query.answer("Cancelled")

# -------------------------------
# MAIN COMMAND (REWRITTEN - Balanced)
# -------------------------------
# @Client.on_message(filters.command("fixmetadata") & filters.private & CustomFilters.owner, group=10)
async def fix_metadata_handler(_, message):
    global CANCEL_REQUESTED
    CANCEL_REQUESTED = False

    # -------------------------
    # Gather totals quickly (non-blocking)
    # -------------------------
    total_movies = 0
    total_tv = 0
    for i in range(1, db.current_db_index + 1):
        key = f"storage_{i}"
        total_movies += await db.dbs[key]["movie"].count_documents({})
        total_tv += await db.dbs[key]["tv"].count_documents({})

    TOTAL = total_movies + total_tv
    DONE = 0
    start_time = time.time()

    status = await message.reply_text(
        "⏳ Initializing metadata fixing...",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_fix")]
        ])
    )

    # -------------------------
    # Tunables
    # -------------------------
    CONCURRENCY = 20                              
    TASK_BATCH = CONCURRENCY * 2                  
    PROGRESS_INTERVAL = 5.0                       

    semaphore = asyncio.Semaphore(CONCURRENCY)
    meta_cache = {}
    last_progress_edit = start_time

    async def cached_fetch_movie(title, year, default_id, encoded_string=None, quality=None):
        if default_id:
            k = ("movie", str(default_id))
        else:
            k = ("movie", f"title::{title or ''}::year::{year or ''}")

        if k in meta_cache:
            return meta_cache[k]

        async with semaphore:
            try:
                meta = await fetch_movie_metadata(title=title, encoded_string=encoded_string, year=year, quality=quality, default_id=default_id)
            except Exception as e:
                LOGGER.exception(f"fetch_movie_metadata error for {title} ({default_id}): {e}")
                meta = None

        meta_cache[k] = meta
        return meta

    async def cached_fetch_tv(title, season, episode, year, default_id, encoded_string=None, quality=None):
        if default_id:
            k = ("tv", str(default_id), int(season), int(episode))
        else:
            k = ("tv", f"title::{title or ''}::year::{year or ''}", int(season), int(episode))

        if k in meta_cache:
            return meta_cache[k]

        async with semaphore:
            try:
                meta = await fetch_tv_metadata(title=title, season=season, episode=episode,
                                               encoded_string=encoded_string, year=year, quality=quality, default_id=default_id)
            except Exception as e:
                LOGGER.exception(f"fetch_tv_metadata error for {title} S{season}E{episode} ({default_id}): {e}")
                meta = None

        meta_cache[k] = meta
        return meta


    async def _safe_update_movie(collection, movie_doc):
        nonlocal DONE, last_progress_edit

        if CANCEL_REQUESTED:
            return
        try:
            doc_id = movie_doc.get("_id")
            imdb_id = movie_doc.get("imdb_id")
            tmdb_id = movie_doc.get("tmdb_id")
            title = movie_doc.get("title")
            year = movie_doc.get("release_year")

            meta_primary = None
            meta_secondary = None

            if imdb_id:
                meta_primary = await cached_fetch_movie(title, year, imdb_id)
                fetched_tmdb = meta_primary.get("tmdb_id") if meta_primary else None
                if (tmdb_id or fetched_tmdb) and (not all_fields_present(meta_primary)):
                    meta_secondary = await cached_fetch_movie(title, year, (tmdb_id or fetched_tmdb))
            elif tmdb_id:
                meta_primary = await cached_fetch_movie(title, year, tmdb_id)
                fetched_imdb = meta_primary.get("imdb_id") if meta_primary else None
                if fetched_imdb and (not all_fields_present(meta_primary)):
                    meta_secondary = await cached_fetch_movie(title, year, fetched_imdb)
            else:
                meta_primary = await cached_fetch_movie(title, year, None)
                if meta_primary:
                    fetched_imdb = meta_primary.get("imdb_id")
                    fetched_tmdb = meta_primary.get("tmdb_id")
                    if fetched_imdb and (not all_fields_present(meta_primary)):
                        meta_secondary = await cached_fetch_movie(title, year, fetched_imdb)
                    elif fetched_tmdb and (not all_fields_present(meta_primary)):
                        meta_secondary = await cached_fetch_movie(title, year, fetched_tmdb)

            update_query = {}
            current = dict(movie_doc)

            api_map = {
                "imdb_id": "imdb_id",
                "tmdb_id": "tmdb_id",
                "rate": "rating",
                "cast": "cast",
                "description": "description",
                "genres": "genres",
                "poster": "poster",
                "backdrop": "backdrop",
                "runtime": "runtime",
                "logo": "logo"
            }

            for meta in (meta_primary, meta_secondary):
                if not meta:
                    continue
                for api_key, db_key in api_map.items():
                    new_val = meta.get(api_key)
                    if new_val is None:
                        continue

                    if db_key == "rating":
                        is_empty = (not current.get(db_key)) or current.get(db_key) == 0
                    else:
                        is_empty = not current.get(db_key)
                    # # If want to add missing data
                    # if is_empty and new_val:
                    #     update_query[db_key] = new_val
                    #     current[db_key] = new_val
                    # # If want to add complete data
                    if new_val is not None:
                        update_query[db_key] = new_val
                        current[db_key] = new_val


            if update_query:
                filter_q = {"_id": doc_id} if doc_id else {"imdb_id": imdb_id}
                try:
                    await collection.update_one(filter_q, {"$set": update_query})
                except Exception as e:
                    LOGGER.exception(f"DB update failed for movie {title}: {e}")

            DONE += 1

            now = time.time()
            if now - last_progress_edit > PROGRESS_INTERVAL:
                last_progress_edit = now
                try:
                    await status.edit_text(
                        f"⏳ Fixing metadata...\n{progress_bar(DONE, TOTAL)}\n⏱ Elapsed: {format_eta(now - start_time)}"
                    )
                except Exception:
                    pass

        except Exception as e:
            LOGGER.exception(f"Error updating movie {movie_doc.get('title')}: {e}")
            DONE += 1

    async def _safe_update_tv(collection, tv_doc):
        nonlocal DONE, last_progress_edit

        if CANCEL_REQUESTED:
            return

        try:
            doc_id = tv_doc.get("_id")
            imdb_id = tv_doc.get("imdb_id")
            tmdb_id = tv_doc.get("tmdb_id")
            title = tv_doc.get("title")
            year = tv_doc.get("release_year")

            meta_primary = None
            meta_secondary = None

            if imdb_id:
                meta_primary = await cached_fetch_tv(title, 1, 1, year, imdb_id)
                fetched_tmdb = meta_primary.get("tmdb_id") if meta_primary else None
                if (tmdb_id or fetched_tmdb) and (not all_fields_present(meta_primary)):
                    meta_secondary = await cached_fetch_tv(title, 1, 1, year, (tmdb_id or fetched_tmdb))
            elif tmdb_id:
                meta_primary = await cached_fetch_tv(title, 1, 1, year, tmdb_id)
                fetched_imdb = meta_primary.get("imdb_id") if meta_primary else None
                if fetched_imdb and (not all_fields_present(meta_primary)):
                    meta_secondary = await cached_fetch_tv(title, 1, 1, year, fetched_imdb)
            else:
                meta_primary = await cached_fetch_tv(title, 1, 1, year, None)
                if meta_primary:
                    fetched_imdb = meta_primary.get("imdb_id")
                    fetched_tmdb = meta_primary.get("tmdb_id")
                    if fetched_imdb and (not all_fields_present(meta_primary)):
                        meta_secondary = await cached_fetch_tv(title, 1, 1, year, fetched_imdb)
                    elif fetched_tmdb and (not all_fields_present(meta_primary)):
                        meta_secondary = await cached_fetch_tv(title, 1, 1, year, fetched_tmdb)

            update_query = {}
            current = dict(tv_doc)
            api_map = {
                "imdb_id": "imdb_id",
                "tmdb_id": "tmdb_id",
                "rate": "rating",
                "cast": "cast",
                "description": "description",
                "genres": "genres",
                "poster": "poster",
                "backdrop": "backdrop",
                "runtime": "runtime",
                "logo": "logo"
            }

            for meta in (meta_primary, meta_secondary):
                if not meta:
                    continue
                for api_key, db_key in api_map.items():
                    new_val = meta.get(api_key)
                    if new_val is None:
                        continue
                    if db_key == "rating":
                        is_empty = (not current.get(db_key)) or current.get(db_key) == 0
                    else:
                        is_empty = not current.get(db_key)
                    # # If want to add missing data
                    # if is_empty and new_val:
                    #     update_query[db_key] = new_val
                    #     current[db_key] = new_val
                    # # If want to add complete data
                    if new_val is not None:
                        update_query[db_key] = new_val
                        current[db_key] = new_val


            if update_query:
                filter_q = {"_id": doc_id} if doc_id else {"imdb_id": imdb_id}
                try:
                    await collection.update_one(filter_q, {"$set": update_query})
                except Exception as e:
                    LOGGER.exception(f"DB update failed for TV {title}: {e}")

            final_imdb = current.get("imdb_id")
            if not final_imdb:
                DONE += 1
                return

            ep_tasks = []
            for season in tv_doc.get("seasons", []):
                s_num = season.get("season_number")
                for ep in season.get("episodes", []):
                    e_num = ep.get("episode_number")

                    # skip if episode appears complete
                    if ep.get("overview") and ep.get("released") and ep.get("episode_backdrop"):
                        continue

                    async def ep_task(sn=s_num, en=e_num):
                        try:
                            meta = await cached_fetch_tv(title, sn, en, year, final_imdb)
                            if not meta:
                                return

                            ep_update = {}
                            if meta.get("episode_overview"):
                                ep_update["seasons.$[s].episodes.$[e].overview"] = meta["episode_overview"]
                            if meta.get("episode_released"):
                                ep_update["seasons.$[s].episodes.$[e].released"] = meta["episode_released"]
                            if meta.get("episode_backdrop"):
                                ep_update["seasons.$[s].episodes.$[e].episode_backdrop"] = meta["episode_backdrop"]

                            if ep_update:
                                filt = {"_id": doc_id} if doc_id else {"imdb_id": final_imdb}
                                await collection.update_one(
                                    filt,
                                    {"$set": ep_update},
                                    array_filters=[
                                        {"s.season_number": sn},
                                        {"e.episode_number": en}
                                    ]
                                )
                        except Exception as e:
                            LOGGER.exception(f"Error updating episode {title} S{sn}E{en}: {e}")

                    ep_tasks.append(ep_task())

            for i in range(0, len(ep_tasks), TASK_BATCH):
                if CANCEL_REQUESTED:
                    break
                batch = ep_tasks[i:i+TASK_BATCH]
                running = [asyncio.create_task(t) for t in batch]
                await asyncio.gather(*running, return_exceptions=True)

            DONE += 1

            now = time.time()
            if now - last_progress_edit > PROGRESS_INTERVAL:
                last_progress_edit = now
                try:
                    await status.edit_text(
                        f"⏳ Fixing metadata...\n{progress_bar(DONE, TOTAL)}\n⏱ Elapsed: {format_eta(now - start_time)}"
                    )
                except Exception:
                    pass

        except Exception as e:
            LOGGER.exception(f"Error updating TV show {tv_doc.get('title')}: {e}")
            DONE += 1

    def all_fields_present(meta: dict) -> bool:
        if not meta:
            return False

        if not (meta.get("poster") or meta.get("backdrop")):
            return False

        has_desc = meta.get("description") or meta.get("genres") or meta.get("cast")
        if not has_desc:
            return False

        if meta.get("rate") in [0, None]:
            return False

        if meta.get("runtime") in [0, None]:
            return False

        return True



    async def update_movies():
        tasks = []
        for i in range(1, db.current_db_index + 1):
            if CANCEL_REQUESTED:
                break
            collection = db.dbs[f"storage_{i}"]["movie"]
            cursor = collection.find({})
            async for movie in cursor:
                if CANCEL_REQUESTED:
                    break
                tasks.append(_safe_update_movie(collection, movie))
                if len(tasks) >= TASK_BATCH:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def update_tv_shows():
        tasks = []
        for i in range(1, db.current_db_index + 1):
            if CANCEL_REQUESTED:
                break
            collection = db.dbs[f"storage_{i}"]["tv"]
            cursor = collection.find({})
            async for tv in cursor:
                if CANCEL_REQUESTED:
                    break
                tasks.append(_safe_update_tv(collection, tv))
                if len(tasks) >= TASK_BATCH:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        await asyncio.gather(update_movies(), update_tv_shows())
    except Exception as e:
        LOGGER.exception(f"Error in fix_metadata run: {e}")

    if CANCEL_REQUESTED:
        try:
            await status.edit_text("❌ Metadata fixing cancelled by user.")
        except Exception:
            pass
        return

    elapsed = time.time() - start_time
    try:
        await status.edit_text(
            f"🎉 **Metadata Fix Completed!**\n"
            f"{progress_bar(DONE, TOTAL)}\n"
            f"⏱ Time Taken: {format_eta(elapsed)}"
        )
    except Exception:
        pass
