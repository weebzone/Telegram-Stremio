from __future__ import annotations

from time import time

from fastapi import HTTPException

from Backend import StartTime, __version__, db
from Backend.helper.system.analytics import get_activity_overview
from Backend.helper.core.passwords import verify_password
from Backend.helper.telegram.pyro import get_readable_time
from Backend.helper.settings.manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot, multi_clients, work_loads

async def get_system_stats_api():
    try:
        db_stats = await db.get_database_stats()
        total_movies, total_tv_shows = db.content_totals(db_stats)
        api_tokens = await db.get_all_api_tokens()
        
        return {
            "server_status": "running",
            "uptime": get_readable_time(time() - StartTime),
            "telegram_bot": f"@{StreamBot.username}" if StreamBot and StreamBot.username else "@StreamBot",
            "connected_bots": len(multi_clients),
            "version": __version__,
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "databases": db_stats,
            "total_databases": len(db_stats),
            "current_db_index": db.current_db_index,
            "api_tokens": api_tokens
        }
    except Exception as e:
        print(f"System Stats API Error: {e}")
        return {
            "server_status": "error", 
            "error": str(e)
        }


#----- Expand stored gradient cover paths into full URLs for UI responses

#----- Admin stats
async def get_admin_stats_api() -> dict:
    cache_size = sum(len(s._file_id_cache) for s in _streamer_by_client.values())

    bot_stats = []
    for client_index in multi_clients:
        load = work_loads.get(client_index, 0)
        failures = client_failures.get(client_index, 0)
        mbps = client_avg_mbps.get(client_index, 0.0)

        status = "healthy"
        if failures > 5:
            status = "degraded"
        if failures > 15:
            status = "failing"

        bot_stats.append({
            "client_index": client_index,
            "display_name": "Userbot" if client_index < 0 else f"Bot {client_index + 1}",
            "dc": client_dc_map.get(client_index),
            "current_load": load,
            "failures": failures,
            "avg_mbps": round(mbps, 2),
            "status": status
        })

    return {
        "cache_size": cache_size,
        "total_bots": len(multi_clients),
        "bot_workloads": bot_stats
    }


#----- Clear the FileId cache across all active streamers
async def clear_cache_api() -> dict:
    total_cleared = sum(len(s._file_id_cache) for s in _streamer_by_client.values())
    for streamer in _streamer_by_client.values():
        streamer._file_id_cache.clear()
    LOGGER.info(f"Admin cleared the FileId cache ({total_cleared} items purged across {len(_streamer_by_client)} clients).")

    return {"status": "success", "message": f"{total_cleared} cached items cleared."}


#----- List dead links recorded in the DB
async def get_dead_links_api() -> dict:
    try:
        dead_links = await db.get_all_dead_links()
        return {"status": "success", "data": dead_links}
    except Exception as e:
        return {"status": "error", "message": str(e)}


#----- Recent stream analytics
async def get_stream_analytics_api() -> dict:
    try:
        data = await db.get_stream_analytics(limit=200)
        return {"status": "success", "data": data}
    except Exception as e:
        LOGGER.error(f"Stream analytics API error: {e}")
        return {"status": "error", "message": str(e)}


#----- Purge all stream analytics records
async def clear_stream_analytics_api() -> dict:
    try:
        result = await db.dbs["tracking"]["stream_analytics"].delete_many({})
        LOGGER.info(f"Admin cleared stream analytics ({result.deleted_count} records deleted).")

        return {
            "status": "success",
            "message": f"{result.deleted_count} analytics records cleared."
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}



async def get_user_activity_api(page: int = 1, per_page: int = 5):
    try:
        return await get_activity_overview(page, per_page)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



async def setup_status_api() -> dict:
    s = SettingsManager.current()
    checks = [
        {"key": "tmdb", "label": "TMDB API key", "done": bool(s.tmdb_api),
         "hint": "Powers automatic poster & metadata matching."},
        {"key": "tvdb", "label": "TVDB API key", "done": bool(s.tvdb_api),
         "hint": "Improves TV show matching; used after TMDB / with anime pipelines."},
        {"key": "channels", "label": "AUTH channel added", "done": len(s.auth_channels) > 0,
         "hint": "The channel(s) the bot indexes and streams from."},
        {"key": "base_url", "label": "Base URL set", "done": bool(s.base_url),
         "hint": "Stremio uses this public address to reach your streams."},
        {"key": "password", "label": "Admin password changed", "done": not verify_password("admin", s.admin_password),
         "hint": "Change the default admin / admin login for security."},
    ]
    done = sum(1 for c in checks if c["done"])
    return {"status": "success", "data": {
        "checks": checks, "done": done, "total": len(checks), "complete": done == len(checks),
    }}


