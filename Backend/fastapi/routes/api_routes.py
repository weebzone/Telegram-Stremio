import asyncio
import asyncio
import json
import os
import random
import secrets
import shutil
from datetime import datetime
from time import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

import Backend
from Backend import StartTime, __version__, db
from Backend.fastapi.routes.stream_routes import _streamer_by_client
from Backend.fastapi.routes.stremio_routes import invalidate_membership_cache
from Backend.helper.auto_catalog import (
    get_auto_catalog_settings,
    get_auto_catalog_sync_status,
    start_auto_catalog_sync_background,
    start_single_media_catalog_sync,
    update_auto_catalog_settings,
)
from Backend.helper.backup import export_config, import_config
from Backend.helper.custom_dl import ByteStreamer, _speed_test_single_client, run_speed_test
from Backend.helper.encrypt import decode_string, encode_string
from Backend.helper.health import run_health_checks
from Backend.helper.manual_add import resolve_telegram_message
from Backend.helper.requests_manager import (
    delete_request,
    list_requests,
    popular_pending,
    search_titles,
    set_status,
    submit_request,
)
from Backend.helper.metadata import (
    extract_default_id,
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    gradient_cover_path,
    resolve_cover_url,
    search_any_candidates,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.passwords import hash_password, verify_password
from Backend.helper.pyro import get_readable_file_size, get_readable_time
from Backend.helper.scan_manager import dbcheck_manager, duplicate_manager, scan_manager
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.split_files import strip_part_suffix
from Backend.helper.subtitles import (
    list_languages,
    list_title_subtitles,
    manual_ingest_subtitle,
    remove_subtitle,
    resolve_subtitle_message,
)
from Backend.logger import LOGGER
from Backend.pyrofork.bot import (
    StreamBot,
    client_avg_mbps,
    client_dc_map,
    client_failures,
    multi_clients,
    work_loads,
)


#----- System stats
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
def _resolve_covers(items) -> None:
    for item in items or []:
        for key in ("poster", "backdrop"):
            if item.get(key):
                item[key] = resolve_cover_url(item[key])


#----- Media management
async def list_media_api(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100),
    custom: bool = Query(False)
):
    try:
        key = "movies" if media_type == "movie" else "tv_shows"
        #----- Custom (manually added) titles carry a negative synthetic tmdb_id
        extra_filter = {"tmdb_id": {"$lt": 0}} if custom else None
        if search:
            result = await db.search_documents(search, page, page_size)
            filtered_results = [
                item for item in result['results']
                if item.get('media_type') == media_type and (not custom or int(item.get('tmdb_id') or 0) < 0)
            ]
            total_filtered = len(filtered_results)
            start_index = (page - 1) * page_size
            resp = {
                "total_count": total_filtered,
                "current_page": page,
                "total_pages": (total_filtered + page_size - 1) // page_size,
                key: filtered_results[start_index:start_index + page_size],
            }
        elif media_type == "movie":
            resp = await db.sort_movies([], page, page_size, extra_filter=extra_filter)
        else:
            resp = await db.sort_tv_shows([], page, page_size, extra_filter=extra_filter)
        _resolve_covers(resp.get(key))
        return resp
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_media_api(
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    try:
        media_type_formatted = "Movie" if media_type == "movie" else "Series"
        result = await db.delete_document(media_type_formatted, tmdb_id, db_index)
        if result:
            return {"message": "Media deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def update_media_api(
    request: Request,
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    try:
        update_data = await request.json()
        if 'rating' in update_data and update_data['rating']:
            try:
                update_data['rating'] = float(update_data['rating'])
            except (ValueError, TypeError):
                update_data['rating'] = 0.0
        
        if 'release_year' in update_data and update_data['release_year']:
            try:
                update_data['release_year'] = int(update_data['release_year'])
            except (ValueError, TypeError):
                pass
        if 'genres' in update_data:
            if isinstance(update_data['genres'], str):
                update_data['genres'] = [g.strip() for g in update_data['genres'].split(',') if g.strip()]
            elif not isinstance(update_data['genres'], list):
                update_data['genres'] = []
        
        if 'languages' in update_data:
            if isinstance(update_data['languages'], str):
                update_data['languages'] = [l.strip() for l in update_data['languages'].split(',') if l.strip()]
            elif not isinstance(update_data['languages'], list):
                update_data['languages'] = []
        if media_type == "movie":
            if 'runtime' in update_data and update_data['runtime']:
                try:
                    update_data['runtime'] = int(update_data['runtime'])
                except (ValueError, TypeError):
                    pass
        elif media_type == "tv":
            if 'total_seasons' in update_data and update_data['total_seasons']:
                try:
                    update_data['total_seasons'] = int(update_data['total_seasons'])
                except (ValueError, TypeError):
                    pass
            
            if 'total_episodes' in update_data and update_data['total_episodes']:
                try:
                    update_data['total_episodes'] = int(update_data['total_episodes'])
                except (ValueError, TypeError):
                    pass
        update_data = {k: v for k, v in update_data.items() if v != ""}
        result = await db.update_document(media_type, tmdb_id, db_index, update_data)
        if result:
            return {"message": "Media updated successfully"}
        else:
            raise HTTPException(status_code=404, detail="Media not found or no changes made")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def get_media_details_api(
    tmdb_id: int,
    db_index: int,
    media_type: str = Query(regex="^(movie|tv)$")
):
    try:
        result = await db.get_document(media_type, tmdb_id, db_index)
        if result:
            return result
        else:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_movie_quality_api(tmdb_id: int, db_index: int, id: str):
    try:
        result = await db.delete_movie_quality(tmdb_id, db_index, id)
        if result:
            return {"message": "Quality deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Quality not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_tv_quality_api(
    tmdb_id: int, db_index: int, season: int, episode: int, id: str
):
    try:
        result = await db.delete_tv_quality(tmdb_id, db_index, season, episode, id)
        if result:
            return {"message": "deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Quality not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_tv_episode_api(
    tmdb_id: int, db_index: int, season: int, episode: int
):
    try:
        result = await db.delete_tv_episode(tmdb_id, db_index, season, episode)
        if result:
            return {"message": "Episode deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Episode not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def delete_tv_season_api(tmdb_id: int, db_index: int, season: int):
    try:
        result = await db.delete_tv_season(tmdb_id, db_index, season)
        if result:
            return {"message": "Season deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Season not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Token management
#----- Parse a GB-limit value into a positive float, or None
def _parse_limit(val):
    try:
        v = float(val)
        return v if v > 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


async def create_token_api(payload: dict):
    try:
        token_name = payload.get("name")
        if not token_name:
            raise HTTPException(status_code=400, detail="Token name is required")

        new_token = await db.add_api_token(
            token_name,
            _parse_limit(payload.get("daily_limit_gb")),
            _parse_limit(payload.get("monthly_limit_gb")),
            subscription_exempt=bool(payload.get("subscription_exempt")),
        )
        return new_token
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Toggle a token's lifetime (subscription-exempt) flag
async def set_token_lifetime_api(token: str, payload: dict) -> dict:
    exempt = bool(payload.get("subscription_exempt"))
    if not await db.set_token_lifetime(token, exempt):
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "success", "subscription_exempt": exempt}


#----- Set/extend/reduce a token's own expiry (subscription-off mode).
#----- Optionally attach a Telegram user id at the same time.
async def set_token_expiry_api(token: str, payload: dict) -> dict:
    user_id = payload.get("user_id")
    if user_id not in (None, "", 0, "0"):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid Telegram user id.")
        #----- Enforces one-user-one-token + pulls the real Telegram name
        await link_token_user_api(token, uid)

    action = str(payload.get("action") or "set")
    days = int(payload.get("days") or 0)
    result = await db.update_token_expiry(token, action, days)
    if not result:
        raise HTTPException(status_code=404, detail="Token not found.")
    return {"status": "success", "expires_at": result.get("expires_at").isoformat() if result.get("expires_at") else None}


#----- How many tokens would stop working if subscription mode is enabled
async def subscription_preflight_api() -> dict:
    return {"status": "success", "uncovered": await db.count_uncovered_tokens()}


#----- Relabel "User <id>" placeholder subscribers with their real Telegram name
async def backfill_subscriber_names_api() -> dict:
    users = await db.get_all_subscribers()
    updated = 0
    for u in users:
        uid = u.get("_id")
        if uid is None or (u.get("first_name") or "") != f"User {uid}":
            continue
        name = await _fetch_tg_name(uid)
        if name and name != f"User {uid}":
            await db.update_subscriber_name(uid, name)
            updated += 1
    return {"status": "success", "updated": updated, "message": f"{updated} name(s) updated."}


#----- Mark all tokens that aren't linked to a user as lifetime
async def grant_lifetime_api() -> dict:
    count = await db.grant_lifetime_to_unlinked()
    return {"status": "success", "updated": count, "message": f"{count} token(s) marked as lifetime."}

async def update_token_limits_api(token: str, payload: dict):
    try:
        daily_limit = payload.get("daily_limit_gb")
        monthly_limit = payload.get("monthly_limit_gb")

        await db.update_api_token_limits(
            token,
            _parse_limit(daily_limit),
            _parse_limit(monthly_limit)
        )
        return {"message": "Limits updated successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Speed test
#----- Decode a quality_id into (chat_id, msg_id); split files use the first part
async def _resolve_speed_test_target(quality_id: str):
    decoded = await decode_string(quality_id)
    target = decoded["parts"][0] if decoded.get("parts") else decoded
    msg_id = target.get("msg_id")
    raw_cid = target.get("chat_id")
    if not msg_id or not raw_cid:
        return None, None, decoded
    return int(f"-100{raw_cid}"), int(msg_id), decoded


#----- Run a parallel download speed test across all connected clients
async def speed_test_api(
    quality_id: str = Query(..., description="Encoded quality ID from DB"),
    tmdb_id: int = Query(...),
    db_index: int = Query(...),
    media_type: str = Query(..., regex="^(movie|tv)$"),
):
    try:
        chat_id, msg_id, decoded = await _resolve_speed_test_target(quality_id)
        if not chat_id or not msg_id:
            raise HTTPException(
                status_code=422,
                detail=f"Decoded quality data is missing msg_id or chat_id. Decoded: {decoded}"
            )

        results = await run_speed_test(chat_id, msg_id)
        return {"results": results, "total_clients_tested": len(results)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- SSE speed test streaming per-client results as they finish
async def speed_test_stream_api(
    quality_id: str,
    tmdb_id: int,
    db_index: int,
    media_type: str,
):

    async def event_generator():
        try:
            chat_id, msg_id, decoded = await _resolve_speed_test_target(quality_id)
            if not chat_id or not msg_id:
                payload = json.dumps({"type": "error", "message": f"Cannot decode quality_id. Got: {decoded}"})
                yield f"data: {payload}\n\n"
                return
        except Exception as exc:
            payload = json.dumps({"type": "error", "message": str(exc)})
            yield f"data: {payload}\n\n"
            return

        total = len(multi_clients)
        if total == 0:
            payload = json.dumps({"type": "error", "message": "No bot clients connected"})
            yield f"data: {payload}\n\n"
            return

        #----- Resolve the FileId to report the target DC
        target_dc = "?"
        try:
            primary_client = multi_clients.get(0) or next(iter(multi_clients.values()))
            streamer = ByteStreamer(primary_client)
            file_id = await streamer.get_file_properties(chat_id, int(msg_id))
            target_dc = file_id.dc_id
        except Exception:
            pass

        #----- Initial start event so the frontend can build its table
        yield f"data: {json.dumps({'type': 'start', 'total': total, 'target_dc': target_dc})}\n\n"

        #----- Run all clients in parallel, feeding results into a queue
        queue: asyncio.Queue = asyncio.Queue()

        async def run_one(client, idx):
            async def on_progress(prog_data):
                await queue.put({"type": "progress", "data": prog_data})

            result = await _speed_test_single_client(
                client, idx, chat_id, int(msg_id), progress_callback=on_progress
            )
            await queue.put({"type": "result", "data": result})

        tasks = [
            asyncio.create_task(run_one(client, idx))
            for idx, client in multi_clients.items()
        ]

        completed = 0
        while completed < total:
            msg = await queue.get()

            if msg["type"] == "progress":
                payload = json.dumps(msg)
                yield f"data: {payload}\n\n"

            elif msg["type"] == "result":
                completed += 1
                payload = json.dumps({
                    "type": "result",
                    "data": msg["data"],
                    "completed": completed,
                    "total": total,
                })
                yield f"data: {payload}\n\n"

        await asyncio.gather(*tasks, return_exceptions=True)
        yield f"data: {json.dumps({'type': 'done', 'total': total})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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


#----- Public: search titles to request (by name, IMDb id or TMDB id)
async def request_search_api(q: str) -> dict:
    try:
        return {"status": "success", "data": await search_titles(q)}
    except Exception as e:
        LOGGER.error(f"Request search error: {e}")
        return {"status": "error", "message": str(e), "data": []}


#----- Public: submit a request for a title
async def request_submit_api(payload: dict, client_ip: str) -> dict:
    result = await submit_request(
        media_type=payload.get("media_type"),
        tmdb_id=payload.get("tmdb_id"),
        imdb_id=payload.get("imdb_id"),
        title=payload.get("title"),
        year=payload.get("year"),
        poster=payload.get("poster"),
        client_ip=client_ip,
    )
    return {"status": "success" if result.get("ok") else "error", **result}


#----- Public: most-requested pending titles
async def request_popular_api() -> dict:
    try:
        return {"status": "success", "data": await popular_pending()}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}


#----- Admin: list all content requests
async def get_requests_api() -> dict:
    try:
        return {"status": "success", "data": await list_requests()}
    except Exception as e:
        LOGGER.error(f"Requests API error: {e}")
        return {"status": "error", "message": str(e)}


#----- Admin: uploaded / denied / banned / pending
async def update_request_api(request_id: str, payload: dict) -> dict:
    new_status = str(payload.get("status", "")).strip()
    doc = await set_status(request_id, new_status)
    if not doc:
        raise HTTPException(status_code=404, detail="Request not found or invalid status.")
    return {"status": "success", "data": doc}


async def delete_request_api(request_id: str) -> dict:
    if not await delete_request(request_id):
        raise HTTPException(status_code=404, detail="Request not found.")
    return {"status": "success", "message": "Request deleted."}


#----- Admin subscription management
async def get_subscription_plans_api() -> dict:
    try:
        plans = await db.get_subscription_plans()
        return {"status": "success", "data": plans}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def add_subscription_plan_api(payload: dict) -> dict:
    try:
        days = int(payload.get("days", 0))
        price = float(payload.get("price", 0.0))
        if days <= 0 or price < 0:
            raise HTTPException(status_code=400, detail="Invalid plan parameters")
            
        plan_id = await db.add_subscription_plan(days, price)
        if plan_id:
            return {"status": "success", "message": "Plan added successfully", "plan_id": plan_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to add plan")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def update_subscription_plan_api(plan_id: str, payload: dict) -> dict:
    try:
        days = int(payload.get("days", 0))
        price = float(payload.get("price", 0.0))
        if days <= 0 or price < 0:
             raise HTTPException(status_code=400, detail="Invalid plan parameters")
             
        success = await db.update_subscription_plan(plan_id, days, price)
        if success:
             return {"status": "success", "message": "Plan updated successfully"}
        else:
             raise HTTPException(status_code=404, detail="Plan not found or update failed")
    except HTTPException:
         raise
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))

async def delete_subscription_plan_api(plan_id: str) -> dict:
    try:
        success = await db.delete_subscription_plan(plan_id)
        if success:
            return {"status": "success", "message": "Plan deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Plan not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def get_all_subscribers_api() -> dict:
    try:
        users = await db.get_all_subscribers()
        for u in users:
            u["is_admin"] = db._is_owner(u.get("_id"))
        return {"status": "success", "data": users}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def manage_subscriber_api(user_id: int, payload: dict) -> dict:
    try:
        action = payload.get("action")
        days = int(payload.get("days", 0))

        if action not in ["extend", "reduce", "delete", "remove"]:
            raise HTTPException(status_code=400, detail="Invalid action")

        success = await db.manage_subscriber(user_id, action, days)

        #----- On revoke/remove, kick the user from the group immediately (ban+unban)
        if success and action in ("delete", "remove") and SettingsManager.current().subscription:
            group_id = SettingsManager.current().subscription_group_id
            if group_id:
                try:
                    await StreamBot.ban_chat_member(group_id, user_id)
                    await StreamBot.unban_chat_member(group_id, user_id)
                except Exception as exc:
                    LOGGER.warning(f"Revoke: could not remove user {user_id} from group: {exc}")

        #----- Reflect the change immediately in the stremio membership cache
        if success:
            try:
                invalidate_membership_cache(user_id)
            except Exception:
                pass

        if success:
            verb = {"extend": "extended", "reduce": "reduced", "delete": "revoked", "remove": "removed"}.get(action, "updated")
            return {"status": "success", "message": f"User subscription {verb} successfully"}
        else:
            raise HTTPException(status_code=404, detail="User not found or update failed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Access management
async def get_all_tokens_api() -> dict:
    try:
        tokens = await db.get_all_api_tokens()
        now = datetime.utcnow()
        result = []

        #----- Pre-load subscribers keyed by user_id for O(1) lookup
        subscriber_map = {}
        if SettingsManager.current().subscription:
            try:
                for u in await db.get_all_subscribers():
                    uid = str(u.get("_id"))
                    subscriber_map[uid] = u
            except Exception:
                pass

        #----- Display name, preferring a real name/alias over the "User <id>" placeholder
        def display_name(user, user_id, token_name=None):
            placeholder = f"User {user_id}" if user_id is not None else None
            options = [token_name]
            if user:
                options += [user.get("first_name"), user.get("username")]
            for o in options:
                if o and o != placeholder:
                    return o
            for o in options:
                if o:
                    return o
            return placeholder or "Telegram User"

        sub_on = SettingsManager.current().subscription

        #----- Unified access entry from optional user + token records
        def build_entry(user_id, user, token_doc):
            token_doc = token_doc or {}
            user_found = bool(user)
            sub_status = user.get("subscription_status") if user else None
            is_admin = bool(token_doc.get("is_admin")) or db._is_owner(user_id)
            lifetime = bool(token_doc.get("subscription_exempt"))
            token_str = token_doc.get("token")

            token_expiry = token_doc.get("expires_at")
            user_sub_expiry = user.get("subscription_expiry") if user else None

            #----- Sub OFF: token's own expiry (display only). Sub ON: token expiry is an
            #----- admin grant, otherwise fall back to the subscription's expiry.
            if not sub_on:
                expiry = token_expiry
                is_expired = False
            elif is_admin or lifetime:
                expiry = None
                is_expired = False
            elif token_expiry is not None:
                expiry = token_expiry
                is_expired = token_expiry < now
            elif user_found and sub_status == "active" and user_sub_expiry:
                expiry = user_sub_expiry
                is_expired = user_sub_expiry < now
            else:
                expiry = user_sub_expiry
                is_expired = True

            created = token_doc.get("created_at") or (user.get("created_at") if user else None)
            limits = token_doc.get("limits") or {}
            usage = token_doc.get("usage") or {}
            has_active_sub = sub_on and user_found and sub_status == "active" and bool(user_sub_expiry) and user_sub_expiry > now
            never_expires = not expiry and (is_admin or lifetime or not sub_on)

            return {
                "token": token_str,
                "user_id": user_id,
                "user_name": display_name(user, user_id, token_doc.get("name")),
                "user_found": user_found,
                "is_admin": is_admin,
                "lifetime": lifetime,
                "never_expires": never_expires,
                "has_token": bool(token_str),
                "has_active_sub": has_active_sub,
                "created_at": created.isoformat() if created else None,
                "expires_at": expiry.isoformat() if expiry else None,
                "is_expired": is_expired,
                "sub_status": sub_status,
                "daily_limit_gb": limits.get("daily_limit_gb") or 0,
                "monthly_limit_gb": limits.get("monthly_limit_gb") or 0,
                "daily_bytes": (usage.get("daily") or {}).get("bytes", 0),
                "monthly_bytes": (usage.get("monthly") or {}).get("bytes", 0),
                "addon_url": (
                    f"{SettingsManager.current().base_url}/stremio/{token_str}/manifest.json"
                    if token_str else None
                ),
            }

        seen_user_ids = set()

        #----- 1. Process all existing tokens
        for t in tokens:
            token_user_id = t.get("user_id")

            user = None
            if token_user_id:
                uid_str = str(token_user_id)
                user = subscriber_map.get(uid_str)
                if not user:
                    try:
                        user = await db.get_user(int(token_user_id))
                    except Exception:
                        pass
                seen_user_ids.add(uid_str)

            result.append(build_entry(token_user_id, user, t))

        #----- 2. Add subscribers who have no token
        for uid_str, u in subscriber_map.items():
            if uid_str in seen_user_ids:
                continue
            result.append(build_entry(u.get("_id"), u, None))

        #----- Sort: active-with-token first, active-no-token next, expired last
        result.sort(key=lambda x: (x["is_expired"], not x["has_token"]))
        return {"tokens": result, "subscription": sub_on}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def revoke_token_api(token: str) -> dict:
    try:
        success = await db.revoke_api_token(token)
        if success:
            return {"status": "success", "message": "Token revoked."}
        raise HTTPException(status_code=404, detail="Token not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Assign or extend a subscription for any user_id
async def assign_plan_api(user_id: int, days: int) -> dict:
    try:
        #----- Use the real Telegram name so the Plans page shows it (not "User <id>")
        name = await _fetch_tg_name(user_id)
        #----- 0 / empty days means "never expires"
        if days and days > 0:
            result = await db.assign_subscription(user_id, days, name)
        else:
            result = await db.set_user_never_expires(user_id, name)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Look up a Telegram user's display name via the bot (best-effort)
async def _fetch_tg_name(user_id: int):
    try:
        u = await StreamBot.get_users(user_id)
        if not u:
            return None
        name = (u.first_name or "").strip()
        if getattr(u, "last_name", None):
            name = f"{name} {u.last_name}".strip()
        return name or (u.username or None)
    except Exception:
        return None


#----- Link an orphan token to a Telegram user_id (one user_id = one token)
async def link_token_user_api(token: str, user_id: int) -> dict:
    try:
        existing = await db.get_api_token_by_user(user_id)
        if existing and existing.get("token") == token:
            return {"status": "success", "message": f"Already linked to user {user_id}."}
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"User {user_id} is already linked to token '{existing.get('name')}'. Unlink or delete that token first.",
            )
        #----- Overwrite the token name with the user's real Telegram name when available
        name = await _fetch_tg_name(user_id)
        success = await db.link_token_user(token, user_id, name)
        if success:
            return {"status": "success", "message": f"Token linked to {name or user_id}."}
        raise HTTPException(status_code=404, detail="Token not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Rescan: search TMDB candidates for a title
async def search_media_rescan_api(media_type: str, query: str, year: int | None = None):
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required.")

    if media_type == "movie":
        results = await search_movie_candidates(query=query, year=year)
    elif media_type == "tv":
        results = await search_tv_candidates(query=query)
    else:
        raise HTTPException(status_code=400, detail="Invalid media_type.")

    return {"results": results}


async def apply_media_rescan_api(request: Request, tmdb_id: int, db_index: int, media_type: str):
    body = await request.json()
    selected_id = str(body.get("selected_id") or "").strip()

    if not selected_id:
        raise HTTPException(status_code=400, detail="selected_id is required.")

    current_doc = await db.get_document(media_type, tmdb_id, db_index)
    if not current_doc:
        raise HTTPException(status_code=404, detail="Media not found.")

    if media_type == "movie":
        metadata = await fetch_selected_movie_metadata(selected_id)
    elif media_type == "tv":
        metadata = await fetch_selected_tv_metadata(selected_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid media_type.")

    if not metadata:
        raise HTTPException(status_code=404, detail="Unable to fetch metadata for selected item.")

    updated_doc = await db.replace_media_metadata(
        media_type=media_type,
        tmdb_id=tmdb_id,
        db_index=db_index,
        metadata=metadata,
    )

    if not updated_doc:
        raise HTTPException(status_code=500, detail="Failed to replace media metadata.")

    return {
        "success": True,
        "message": "Metadata rescanned successfully.",
        "redirect_tmdb_id": updated_doc.get("tmdb_id"),
        "db_index": updated_doc.get("db_index", db_index),
        "media_type": media_type,
        "data": updated_doc,
}


#----- Manual add: fetch full metadata for a selected TMDB/IMDB title to autofill the form
async def resolve_manual_metadata_api(media_type: str, selected_id: str) -> dict:
    selected_id = str(selected_id or "").strip()
    if not selected_id:
        raise HTTPException(status_code=400, detail="selected_id is required.")
    mt = _normalize_media_type(media_type)
    data = await (
        fetch_selected_movie_metadata(selected_id) if mt == "movie"
        else fetch_selected_tv_metadata(selected_id)
    )
    if not data:
        raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")
    if data.get("poster"):
        data["poster"] = resolve_cover_url(data["poster"])
    if data.get("backdrop"):
        data["backdrop"] = resolve_cover_url(data["backdrop"])
    return {"metadata": data}


#----- Manual add: resolve a Telegram post link into a streamable file
async def resolve_telegram_api(payload: dict) -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    try:
        data = await resolve_telegram_message(
            client,
            url=payload.get("url"),
            chat_id=payload.get("chat_id"),
            msg_id=payload.get("msg_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")
    return {"status": "success", "data": data}


async def resolve_subtitle_api(payload: dict) -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    try:
        data = await resolve_subtitle_message(
            client, url=payload.get("url"),
            chat_id=payload.get("chat_id"), msg_id=payload.get("msg_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")
    return {"status": "success", "data": data}


async def _resolve_imdb_id(media_type: str, tmdb_id, db_index) -> str:
    if not (tmdb_id and db_index):
        raise HTTPException(status_code=400, detail="tmdb_id and db_index are required.")
    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc or not doc.get("imdb_id"):
        raise HTTPException(status_code=404, detail="Title not found.")
    return doc["imdb_id"]


def list_subtitle_languages_api() -> dict:
    return {"status": "success", "languages": list_languages()}


async def list_subtitles_api(media_type: str, tmdb_id, db_index) -> dict:
    mt = "tv" if media_type in ("tv", "series") else "movie"
    imdb_id = await _resolve_imdb_id(mt, tmdb_id, db_index)
    return {"status": "success", "subtitles": await list_title_subtitles(imdb_id)}


async def add_subtitles_api(payload: dict) -> dict:
    media_type = "tv" if payload.get("media_type") in ("tv", "series") else "movie"
    imdb_id = await _resolve_imdb_id(media_type, payload.get("tmdb_id"), payload.get("db_index"))
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="Provide at least one subtitle to add.")

    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    added, errors = [], []
    for item in items:
        try:
            season = item.get("season") if media_type == "tv" else None
            episode = item.get("episode") if media_type == "tv" else None
            if media_type == "tv" and (not season or not episode):
                raise ValueError("Season and episode are required for series subtitles.")
            resolved = await resolve_subtitle_message(
                client, url=item.get("url"),
                chat_id=item.get("chat_id"), msg_id=item.get("msg_id"),
            )
            doc = await manual_ingest_subtitle(
                imdb_id, media_type, season, episode,
                item.get("lang_code") or resolved["lang_code"],
                resolved["chat_id"], resolved["msg_id"], resolved["name"],
            )
            added.append({
                "name": doc["name"], "lang_label": doc["lang_label"],
                "season": doc["season"], "episode": doc["episode"],
            })
        except ValueError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"Could not add subtitle: {exc}")

    if not added and errors:
        raise HTTPException(status_code=400, detail=" ".join(errors))
    message = f"Added {len(added)} subtitle(s)."
    if errors:
        message += f" {len(errors)} failed: {' '.join(errors)}"
    return {"status": "success", "message": message, "added": added, "errors": errors}


async def remove_subtitle_api(payload: dict) -> dict:
    chat_id = payload.get("chat_id")
    msg_id = payload.get("msg_id")
    if chat_id in (None, "") or msg_id in (None, ""):
        raise HTTPException(status_code=400, detail="chat_id and msg_id are required.")
    if not await remove_subtitle(chat_id, msg_id):
        raise HTTPException(status_code=404, detail="Subtitle not found.")
    return {"status": "success", "message": "Subtitle removed."}


#----- Build a metadata base (title-level fields) from various sources
def _metadata_base(source: dict, from_doc: bool = False) -> dict:
    genres = source.get("genres")
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",") if g.strip()]
    year = source.get("release_year") if from_doc else source.get("year")
    rate = source.get("rating") if from_doc else source.get("rate")
    return {
        "tmdb_id": source.get("tmdb_id"),
        "imdb_id": source.get("imdb_id") or None,
        "title": (source.get("title") or "").strip(),
        "year": int(year) if str(year or "").strip().lstrip("-").isdigit() else 0,
        "rate": float(rate) if str(rate or "").replace(".", "", 1).isdigit() else 0,
        "description": source.get("description") or "",
        "poster": source.get("poster") or "",
        "backdrop": source.get("backdrop") or "",
        "logo": source.get("logo") or "",
        "genres": genres or [],
        "cast": source.get("cast") or [],
        "runtime": str(source.get("runtime") or ""),
        "original_language": source.get("original_language"),
        "origin_country": source.get("origin_country") or [],
    }


_PLACEHOLDER_GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy",
                       "Thriller", "Mystery", "Sci-Fi", "Romance", "Family"]
_PLACEHOLDER_DESCRIPTIONS = [
    "A gripping story full of unexpected twists and turns.",
    "An unforgettable journey that keeps you on the edge of your seat.",
    "A captivating tale of drama, courage and emotion.",
    "An entertaining experience packed with memorable moments.",
    "A thrilling adventure blending heart, action and wonder.",
]


#----- Fill empty optional metadata with random values and a gradient cover path
def _fill_placeholder_metadata(meta: dict) -> None:
    title = meta.get("title") or "Media"
    if not meta.get("poster"):
        meta["poster"] = gradient_cover_path(title, portrait=True)
    if not meta.get("backdrop"):
        meta["backdrop"] = gradient_cover_path(title)
    if not meta.get("genres"):
        meta["genres"] = random.sample(_PLACEHOLDER_GENRES, random.randint(1, 3))
    if not meta.get("rate"):
        meta["rate"] = round(random.uniform(6.0, 8.9), 1)
    if not meta.get("description"):
        meta["description"] = random.choice(_PLACEHOLDER_DESCRIPTIONS)


#----- Manual add: create/append a movie, tv show, season, episode or stream by hand
async def manual_add_media_api(payload: dict) -> dict:
    media_type = payload.get("media_type")
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'.")

    stream = payload.get("stream") or {}
    quality = str(stream.get("quality") or "").strip()
    if not quality:
        raise HTTPException(status_code=400, detail="A quality label (e.g. 1080p) is required.")

    #----- One source = single file, multiple sources = split file parts (in order)
    part_sources = stream.get("parts")
    if not isinstance(part_sources, list) or not part_sources:
        part_sources = [{"url": stream.get("url"), "chat_id": stream.get("chat_id"), "msg_id": stream.get("msg_id")}]
    part_sources = [p for p in part_sources if p and (p.get("url") or (p.get("chat_id") and p.get("msg_id")))]
    if not part_sources:
        raise HTTPException(status_code=400, detail="Provide at least one Telegram message link.")

    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    resolved_parts = []
    for src in part_sources:
        try:
            resolved_parts.append(await resolve_telegram_message(
                client, url=src.get("url"), chat_id=src.get("chat_id"), msg_id=src.get("msg_id"),
            ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Could not read that message: {exc}")

    primary = resolved_parts[0]
    is_split = len(resolved_parts) > 1
    raw_name = (stream.get("name") or primary["name"]).strip()
    name = strip_part_suffix(raw_name) if is_split else raw_name

    #----- Resolve the title-level metadata: existing doc, TMDB/IMDb pick, or manual entry
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    selected_id = str(payload.get("selected_id") or "").strip()

    base = None
    if tmdb_id and db_index:
        doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
        if doc:
            base = _metadata_base(doc, from_doc=True)
    if base is None and selected_id:
        selection = await (
            fetch_selected_movie_metadata(selected_id) if media_type == "movie"
            else fetch_selected_tv_metadata(selected_id)
        )
        if not selection:
            raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")
        base = _metadata_base(selection, from_doc=True)
    if base is None:
        base = _metadata_base(payload.get("manual_metadata") or {})
        if not base["title"]:
            raise HTTPException(status_code=400, detail="A title is required for manual entry.")
        if not base["year"]:
            base["year"] = int(primary.get("upload_year") or 0)

    #----- Brand-new hand-made titles get a negative synthetic id (never collides with TMDB)
    if not base.get("tmdb_id"):
        base["tmdb_id"] = -(secrets.randbelow(2_000_000_000) + 1)
    #----- A synthetic "tg" imdb id is required so Stremio can request meta/streams
    if not base.get("imdb_id"):
        base["imdb_id"] = f"tg{abs(int(base['tmdb_id']))}"
    _fill_placeholder_metadata(base)

    #----- Store the file thumbnail as a base-relative path so it survives base_url changes
    thumb_url = ""
    if primary.get("has_thumb"):
        thumb_enc = await encode_string({"chat_id": int(primary["chat_id"]), "msg_id": int(primary["msg_id"])})
        thumb_url = f"/thumb/{thumb_enc}"

    #----- Split parts share one quality entry via a common group key
    group_key = f"manual:{primary['chat_id']}:{quality}:{secrets.token_hex(6)}" if is_split else None

    tv_extra = {}
    if media_type == "tv":
        try:
            season_number = int(payload.get("season_number"))
            episode_number = int(payload.get("episode_number"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Season and episode numbers are required for TV.")
        tv_extra = {
            "season_number": season_number,
            "episode_number": episode_number,
            "episode_title": (payload.get("episode_title") or "").strip() or f"S{season_number:02d}E{episode_number:02d}",
            "episode_backdrop": payload.get("episode_backdrop") or thumb_url or base.get("backdrop") or "",
            "episode_overview": payload.get("episode_overview") or "",
            "episode_released": payload.get("episode_released") or "",
        }

    for index, part in enumerate(resolved_parts, start=1):
        p_channel = int(part["chat_id"])
        p_msg = int(part["msg_id"])
        encoded = await encode_string({"chat_id": p_channel, "msg_id": p_msg})
        metadata_info = dict(base)
        metadata_info.update({
            "media_type": media_type,
            "quality": quality,
            "encoded_string": encoded,
            "group_key": group_key,
            "part_number": index if is_split else None,
            "is_anime": False,
        })
        metadata_info.update(tv_extra)
        updated_id = await db.insert_media(
            metadata_info, channel=p_channel, msg_id=p_msg,
            size=part["size"], name=name, raw_size=int(part.get("raw_size") or 0),
        )
        if not updated_id:
            raise HTTPException(status_code=500, detail="Failed to add media (validation error).")

    result_tmdb_id = base["tmdb_id"]
    location = await db.find_media_doc(media_type, result_tmdb_id)
    result_db_index = location[1] if location else db.current_db_index

    #----- Assign to selected custom catalogs before triggering auto sync, so any
    #----- exclusivity is stamped on the doc first and auto sync correctly skips it.
    #----- Guarded on `location` so we never add a reference to a non-existent doc.
    catalog_ids = payload.get("catalog_ids") or []
    catalogs_added = []
    if location:
        for cat_id in catalog_ids:
            try:
                cat_id = str(cat_id).strip()
                if not cat_id:
                    continue
                added = await db.add_item_to_custom_catalog(cat_id, int(result_tmdb_id), int(result_db_index), media_type)
                if added:
                    catalog = await db.get_custom_catalog(cat_id)
                    if catalog:
                        catalogs_added.append(catalog.get("name", cat_id))
                        cat_vis = catalog.get("visibility")
                        if cat_vis in ("owner", "tokens"):
                            await db.set_media_visibility(
                                int(result_tmdb_id), int(result_db_index), media_type,
                                cat_vis, catalog.get("allowed_tokens") or []
                            )
                        if catalog.get("exclusive"):
                            await db.mark_item_exclusive(
                                cat_id, int(result_tmdb_id), int(result_db_index),
                                media_type, catalog.get("searchable", False)
                            )
            except Exception:
                pass

    if result_tmdb_id and result_tmdb_id > 0:
        try:
            start_single_media_catalog_sync(db, tmdb_id=result_tmdb_id, media_type=media_type)
        except Exception:
            pass

    message = f"Split stream added ({len(resolved_parts)} parts)." if is_split else "Stream added successfully."
    if catalogs_added:
        message += f" Added to: {', '.join(catalogs_added)}."
    return {
        "status": "success",
        "message": message,
        "tmdb_id": result_tmdb_id,
        "db_index": result_db_index,
        "media_type": media_type,
    }


#----- Custom catalog APIs
def _normalize_media_type(media_type: str) -> str:
    return "tv" if media_type in ["tv", "series"] else "movie"


async def list_custom_catalogs_api(
    tmdb_id: int | None = None,
    db_index: int | None = None,
    media_type: str | None = None,
):
    try:
        catalogs = await db.get_custom_catalogs()
        if tmdb_id is not None and db_index is not None and media_type:
            normalized_type = _normalize_media_type(media_type)
            for catalog in catalogs:
                catalog["contains_current"] = any(
                    int(item.get("tmdb_id", -1)) == int(tmdb_id)
                    and int(item.get("db_index", -1)) == int(db_index)
                    and item.get("media_type") == normalized_type
                    for item in catalog.get("items", []) or []
                )
        return {"catalogs": catalogs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def list_manual_add_catalogs_api():
    try:
        catalogs = await db.get_custom_catalogs()
        filtered = [c for c in catalogs if not c.get("auto")]
        filtered.sort(key=lambda c: (0 if c.get("exclusive") else 1, (c.get("name") or "").lower()))
        return {"catalogs": [
            {"_id": c["_id"], "name": c["name"], "exclusive": bool(c.get("exclusive")),
             "visibility": c.get("visibility", "public")}
            for c in filtered
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_VISIBILITY_MODES = ("public", "tokens", "owner")


#----- Parse a (visibility, allowed_tokens) pair from a request payload
def _clean_visibility(payload: dict):
    visibility = payload.get("visibility")
    if visibility not in _VISIBILITY_MODES:
        visibility = None
    tokens = payload.get("allowed_tokens")
    tokens = [str(t).strip() for t in tokens if str(t).strip()] if isinstance(tokens, list) else []
    return visibility, tokens


async def create_custom_catalog_api(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Catalog name is required.")

    visibility, tokens = _clean_visibility(payload)
    catalog_id = await db.create_custom_catalog(name=name, visibility=visibility or "public", allowed_tokens=tokens)
    if not catalog_id:
        raise HTTPException(status_code=500, detail="Failed to create catalog.")

    catalog = await db.get_custom_catalog(catalog_id)
    return {"message": "Catalog created successfully.", "catalog": catalog}


async def update_custom_catalog_api(catalog_id: str, payload: dict):
    name = payload.get("name")
    visibility, tokens = _clean_visibility(payload)
    exclusive = payload.get("exclusive")
    exclusive = bool(exclusive) if exclusive is not None else None
    searchable = bool(payload.get("searchable"))
    result = await db.update_custom_catalog(
        catalog_id, name=name, visibility=visibility, allowed_tokens=tokens,
        exclusive=exclusive, searchable=searchable,
    )
    if not result:
        catalog = await db.get_custom_catalog(catalog_id)
        if not catalog:
            raise HTTPException(status_code=404, detail="Catalog not found.")
    return {"message": "Catalog updated successfully.", "catalog": await db.get_custom_catalog(catalog_id)}


#----- Set a title's visibility across every catalog it belongs to (used by media edit)
async def set_media_visibility_api(payload: dict):
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = payload.get("media_type")
    if not tmdb_id or not db_index or media_type not in ("movie", "tv", "series"):
        raise HTTPException(status_code=400, detail="tmdb_id, db_index and media_type are required.")

    visibility, tokens = _clean_visibility(payload)
    if not visibility:
        raise HTTPException(status_code=400, detail="A valid visibility is required.")

    count = await db.set_media_visibility(
        int(tmdb_id), int(db_index), _normalize_media_type(media_type), visibility, tokens
    )
    return {
        "status": "success",
        "updated_catalogs": count,
        "message": "Visibility updated — applies to default catalogs and every catalog this title is in.",
    }


#----- Current effective visibility of a title (from the catalogs it belongs to)
async def get_media_visibility_api(tmdb_id: int, db_index: int, media_type: str):
    data = await db.get_media_visibility(int(tmdb_id), int(db_index), _normalize_media_type(media_type))
    return {"visibility": data or {}}


async def delete_custom_catalog_api(catalog_id: str):
    result = await db.delete_custom_catalog(catalog_id)
    if not result:
        raise HTTPException(status_code=404, detail="Catalog not found.")
    return {"message": "Catalog deleted successfully."}


async def get_custom_catalog_items_api(
    catalog_id: str,
    media_type: str | None = None,
    page: int = 1,
    page_size: int = 24,
):
    try:
        data = await db.get_custom_catalog_items(catalog_id, media_type, page, page_size)
        if not data.get("catalog"):
            raise HTTPException(status_code=404, detail="Catalog not found.")
        _resolve_covers(data.get("items"))
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def search_catalog_media_api(
    query: str,
    media_type: str = "movie",
    page: int = 1,
    page_size: int = 12,
):
    query = (query or "").strip()
    if not query:
        return {"results": [], "total_count": 0}

    try:
        result = await db.search_documents(query, page, page_size)
        normalized_type = _normalize_media_type(media_type)
        filtered = [item for item in result.get("results", []) if item.get("media_type") == normalized_type]
        return {"results": filtered, "total_count": len(filtered)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def add_custom_catalog_item_api(catalog_id: str, payload: dict):
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = _normalize_media_type(payload.get("media_type", "movie"))

    if not tmdb_id or not db_index:
        raise HTTPException(status_code=400, detail="tmdb_id and db_index are required.")

    media = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not media:
        raise HTTPException(status_code=404, detail="Media not found.")

    catalog = await db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found.")

    added = await db.add_item_to_custom_catalog(catalog_id, int(tmdb_id), int(db_index), media_type)
    visibility_synced = None
    if added:
        #----- Adding to a hidden/restricted catalog adopts that visibility onto the title
        cat_vis = catalog.get("visibility")
        if cat_vis in ("owner", "tokens"):
            await db.set_media_visibility(
                int(tmdb_id), int(db_index), media_type, cat_vis, catalog.get("allowed_tokens") or []
            )
            visibility_synced = cat_vis
        if catalog.get("exclusive"):
            await db.mark_item_exclusive(catalog_id, int(tmdb_id), int(db_index), media_type, catalog.get("searchable", False))
    message = "Added to catalog." if added else "Already exists in this catalog."
    return {"message": message, "added": added, "visibility_synced": visibility_synced}


async def remove_custom_catalog_item_api(
    catalog_id: str,
    tmdb_id: int,
    db_index: int,
    media_type: str,
):
    catalog = await db.get_custom_catalog(catalog_id)
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog not found.")

    removed = await db.remove_item_from_custom_catalog(
        catalog_id, int(tmdb_id), int(db_index), _normalize_media_type(media_type)
    )
    if not removed:
        return {"message": "Item was not in this catalog.", "removed": False}
    if catalog.get("exclusive"):
        await db.clear_item_exclusive(int(tmdb_id), int(db_index), _normalize_media_type(media_type))
    return {"message": "Removed from catalog.", "removed": True}


async def auto_sync_custom_catalogs_api(force_refresh: bool = False):
    try:
        result = await start_auto_catalog_sync_background(db, force=True, force_refresh=force_refresh)
        return {"message": result.get("message", "Auto sync started."), "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def auto_catalog_sync_status_api():
    try:
        return {"status": await get_auto_catalog_sync_status(db)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def get_auto_catalog_settings_api():
    try:
        return {"settings": await get_auto_catalog_settings(db)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def update_auto_catalog_settings_api(payload: dict):
    try:
        enabled_keys = payload.get("enabled_keys", [])
        if not isinstance(enabled_keys, list):
            raise HTTPException(status_code=400, detail="enabled_keys must be a list.")
        settings = await update_auto_catalog_settings(db, enabled_keys)
        return {"message": "Auto catalog settings saved.", "settings": settings}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




# ─────────────────────────────────────────────────────────────────────────────
# Settings API
# ─────────────────────────────────────────────────────────────────────────────

async def get_settings_api() -> dict:

    data = SettingsManager.current().to_dict()
    #----- Never expose the raw password; only whether one is set
    data["admin_password_set"] = bool(data.get("admin_password"))
    data["admin_password"] = ""
    data["session_secret_set"] = bool(data.get("session_secret"))
    data["session_secret"] = ""

    try:
        data["database_list"] = db.get_database_list()
    except Exception as e:
        LOGGER.error(f"get_settings_api: could not load database list: {e}")
        data["database_list"] = []

    return {"settings": data}


async def update_settings_api(payload: dict) -> dict:

    #----- Empty password string means leave it unchanged
    if "admin_password" in payload and not str(payload["admin_password"]).strip():
        del payload["admin_password"]
    if "session_secret" in payload and not str(payload["session_secret"]).strip():
        del payload["session_secret"]

    #----- Type coercion and validation
    bool_keys = {"replace_mode", "duplicate_protection", "hide_catalog", "subscription", "show_proxy_and_non_proxy_both", "announce_new_content", "delete_on_metadata_fail"}
    for key in bool_keys:
        if key in payload:
            payload[key] = bool(payload[key])

    list_str_keys = {"auth_channels", "multi_tokens", "extra_databases", "global_search_channels", "anime_channels", "manual_channels"}
    for key in list_str_keys:
        if key in payload:
            if not isinstance(payload[key], list):
                raise HTTPException(status_code=400, detail=f"'{key}' must be a list.")
            payload[key] = [str(v).strip() for v in payload[key] if str(v).strip()]

    if "extra_databases" in payload:
        for uri in payload["extra_databases"]:
            if not uri.startswith(("mongodb://", "mongodb+srv://")):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid database URI (must start with mongodb:// or mongodb+srv://): {uri[:30]}…"
                )

    if "approver_ids" in payload:
        if not isinstance(payload["approver_ids"], list):
            raise HTTPException(status_code=400, detail="'approver_ids' must be a list.")
        try:
            payload["approver_ids"] = [int(v) for v in payload["approver_ids"] if str(v).strip()]
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="All approver_ids must be integers.")

    if "subscription_group_id" in payload:
        try:
            payload["subscription_group_id"] = int(payload["subscription_group_id"])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="'subscription_group_id' must be an integer.")
    if "global_search_channels" in payload:
        cleaned = []
        for channel in payload["global_search_channels"]:
            channel = str(channel).strip()
            if not channel:
                continue
            try:
                int(channel)
            except ValueError:
                raise HTTPException(status_code=400,
                    detail=f"Invalid channel id: {channel}"
                    )
            cleaned.append(channel)
        payload["global_search_channels"] = cleaned

    if "anime_channels" in payload:
        cleaned = []
        for channel in payload["anime_channels"]:
            channel = str(channel).strip()
            if not channel:
                continue
            try:
                int(channel.replace("-100", ""))
            except ValueError:
                raise HTTPException(status_code=400,
                    detail=f"Invalid anime channel id: {channel}"
                    )
            cleaned.append(channel)
        payload["anime_channels"] = cleaned

    if "manual_channels" in payload:
        cleaned = []
        for channel in payload["manual_channels"]:
            channel = str(channel).strip()
            if not channel:
                continue
            try:
                int(channel.replace("-100", ""))
            except ValueError:
                raise HTTPException(status_code=400,
                    detail=f"Invalid manual channel id: {channel}"
                    )
            cleaned.append(channel)
        payload["manual_channels"] = cleaned

    #----- The same channel id may not appear in more than one channel field.
    #----- Only AUTH ∩ ANIME is allowed, because an anime channel is an auth channel
    #----- that's flagged as anime (the receiver only indexes files from auth channels).
    _channel_fields = ("auth_channels", "manual_channels", "global_search_channels",
                       "anime_channels", "announcement_channel", "skip_channel")
    if any(field in payload for field in _channel_fields):
        current = SettingsManager.current()

        def _norm_ids(values) -> set:
            if isinstance(values, str):
                values = [values]
            return {str(c).strip().replace("-100", "") for c in (values or []) if str(c).strip()}

        groups = {
            "AUTH": _norm_ids(payload.get("auth_channels", list(current.auth_channels))),
            "MANUAL": _norm_ids(payload.get("manual_channels", list(current.manual_channels))),
            "GLOBAL SEARCH": _norm_ids(payload.get("global_search_channels", list(current.global_search_channels))),
            "ANIME": _norm_ids(payload.get("anime_channels", list(current.anime_channels))),
            "ANNOUNCEMENT": _norm_ids(payload.get("announcement_channel", current.announcement_channel)),
            "SKIP": _norm_ids(payload.get("skip_channel", current.skip_channel)),
        }

        allowed_overlap = frozenset({"AUTH", "ANIME"})
        names = list(groups)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                if frozenset({a, b}) == allowed_overlap:
                    continue
                clash = groups[a] & groups[b]
                if clash:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Channel {', '.join(sorted(clash))} can't be in both {a} and {b} channels — each channel may only belong to one field."
                    )

    #----- Strip whitespace from string fields
    for key in ("tmdb_api", "base_url", "upstream_repo", "upstream_branch",
                "admin_username", "admin_password", "session_secret", "http_proxy_url",
                "payment_instructions", "payment_qr_url", "announcement_channel", "skip_channel"):
        if key in payload and isinstance(payload[key], str):
            payload[key] = payload[key].strip()

    if payload.get("admin_password"):
        payload["admin_password"] = hash_password(payload["admin_password"])

    try:
        reinit_results = await SettingsManager.update(db, payload)
        return {
            "message": "Settings saved successfully.",
            "reinit": reinit_results,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Tools — WebUI replacement for /scan, /rescan, /scanstatus, /cancelscan, /dbcheck
# ─────────────────────────────────────────────────────────────────────────────

#----- Pick a Telegram client capable of fetching channel messages
def _scan_client():
    if StreamBot is not None:
        return StreamBot
    if multi_clients:
        return multi_clients.get(0) or next(iter(multi_clients.values()))
    return None


#----- Configured AUTH channels with friendly names for the picker
async def get_tools_channels_api() -> dict:
    channels = list(SettingsManager.current().auth_channels)
    client = _scan_client()
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


#----- ── Manual upload session (web replacement for the /set bot command) ──

#----- Personal (hand-made) titles get a negative synthetic tmdb_id; real ones are positive
def _is_personal_media(tmdb_id) -> bool:
    try:
        return int(tmdb_id) < 0
    except (TypeError, ValueError):
        return False


#----- Normalize a media document into a compact session-picker result
def _session_result(doc: dict) -> dict:
    mt = doc.get("media_type") or doc.get("type") or "movie"
    mt = "tv" if str(mt).lower() in ("tv", "series") else "movie"
    imdb_id = doc.get("imdb_id") or ""
    tmdb_id = doc.get("tmdb_id")
    selected_id = imdb_id if str(imdb_id).startswith("tt") else (str(tmdb_id) if tmdb_id is not None else "")
    return {
        "tmdb_id": tmdb_id,
        "db_index": doc.get("db_index"),
        "media_type": mt,
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or "",
        "poster": resolve_cover_url(doc.get("poster") or ""),
        "imdb_id": imdb_id,
        "selected_id": selected_id,
        "is_personal": _is_personal_media(tmdb_id),
        "in_library": True,
    }


#----- Search the library, then IMDb/Cinemeta + TMDB, by title or an id/link
async def search_manual_session_api(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"results": []}

    results: list[dict] = []
    seen: set = set()

    def _add(doc: dict) -> None:
        entry = _session_result(doc)
        key = (entry["tmdb_id"], entry["db_index"], entry["media_type"])
        if entry["tmdb_id"] is None or key in seen:
            return
        seen.add(key)
        results.append(entry)

    default_id = extract_default_id(query)
    if default_id:
        try:
            if str(default_id).startswith("tt"):
                doc = await db.get_media_details(default_id)
                if doc:
                    _add(doc)
            else:
                for mt in ("movie", "tv"):
                    location = await db.find_media_doc(mt, int(default_id))
                    if location:
                        found, db_index = location
                        found["media_type"] = mt
                        found["db_index"] = db_index
                        _add(found)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] id lookup failed for '{query}': {e}")

    if not default_id:
        try:
            data = await db.search_documents(query, 1, 20)
            for doc in data.get("results", []):
                _add(doc)
        except Exception as e:
            LOGGER.warning(f"[Manual Session] library search failed for '{query}': {e}")

    library_ids = {(e.get("imdb_id") or "", str(e.get("tmdb_id") or "")) for e in results}
    try:
        online = await search_any_candidates(query)
    except Exception as e:
        LOGGER.warning(f"[Manual Session] online search failed for '{query}': {e}")
        online = []

    for cand in online:
        if not cand.get("selected_id") or not cand.get("title"):
            continue
        imdb_id = cand.get("imdb_id") or ""
        tmdb_id = cand.get("tmdb_id")
        if (imdb_id, str(tmdb_id or "")) in library_ids:
            continue
        results.append({
            "tmdb_id": tmdb_id,
            "db_index": None,
            "media_type": "tv" if cand.get("media_type") == "tv" else "movie",
            "title": cand.get("title") or "",
            "year": cand.get("year") or "",
            "poster": resolve_cover_url(cand.get("poster") or ""),
            "imdb_id": imdb_id,
            "selected_id": str(cand.get("selected_id")),
            "source": cand.get("source"),
            "is_personal": False,
            "in_library": False,
        })

    return {"results": results}


#----- Current active manual upload session (or None)
async def get_manual_session_api() -> dict:
    return {"session": getattr(Backend, "MANUAL_SESSION", None)}


async def _set_online_manual_session(payload: dict, media_type: str, selected_id: str) -> dict:
    if not selected_id:
        raise HTTPException(status_code=400, detail="A library title or a selected id is required.")

    meta = await (
        fetch_selected_movie_metadata(selected_id) if media_type == "movie"
        else fetch_selected_tv_metadata(selected_id)
    )
    if not meta:
        raise HTTPException(status_code=404, detail="Could not fetch metadata for the selected title.")

    imdb_id = meta.get("imdb_id") or ""
    default_id = imdb_id if str(imdb_id).startswith("tt") else selected_id

    season = payload.get("season")
    if media_type == "tv" and season is not None and str(season).strip() != "":
        try:
            season = int(season)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Season must be a number.")
    else:
        season = None

    try:
        display_tmdb = int(meta.get("tmdb_id")) if meta.get("tmdb_id") is not None else 0
    except (TypeError, ValueError):
        display_tmdb = 0

    session = {
        "tmdb_id": display_tmdb,
        "db_index": None,
        "media_type": media_type,
        "title": meta.get("title") or "",
        "year": meta.get("release_year") or "",
        "is_personal": False,
        "kind": "real",
        "default_id": default_id,
        "season": season,
        "episode": None,
        "quality": None,
    }
    Backend.MANUAL_SESSION = session
    return {"status": "success", "session": session}


#----- Activate a manual upload session targeting an existing library title.
#----- Real (TMDB/IMDb) titles parse season/episode/quality from each file; personal
#----- (hand-made) titles need a season for TV since their files carry no metadata.
async def set_manual_session_api(payload: dict) -> dict:
    tmdb_id = payload.get("tmdb_id")
    db_index = payload.get("db_index")
    media_type = _normalize_media_type(payload.get("media_type", "movie"))
    selected_id = str(payload.get("selected_id") or "").strip()
    in_library = payload.get("in_library", True) and tmdb_id is not None and db_index is not None

    if not in_library:
        return await _set_online_manual_session(payload, media_type, selected_id)

    doc = await db.get_document(media_type, int(tmdb_id), int(db_index))
    if not doc:
        raise HTTPException(status_code=404, detail="That title was not found in your library.")

    is_personal = _is_personal_media(tmdb_id)
    session = {
        "tmdb_id": int(tmdb_id),
        "db_index": int(db_index),
        "media_type": media_type,
        "title": doc.get("title") or "",
        "year": doc.get("release_year") or "",
        "is_personal": is_personal,
    }

    if is_personal:
        #----- Personal: files have no usable metadata, so season/episode come from here
        season = payload.get("season")
        episode = payload.get("episode")
        quality = str(payload.get("quality") or "").strip()

        if media_type == "tv":
            if season is None or str(season).strip() == "":
                raise HTTPException(status_code=400, detail="A season number is required for personal TV shows.")
            try:
                season = int(season)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Season must be a number.")
            if episode is not None and str(episode).strip() != "":
                try:
                    episode = int(episode)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="Episode must be a number.")
            else:
                episode = None
        else:
            season = None
            episode = None

        session.update({
            "kind": "personal",
            "default_id": None,
            "season": season,
            "episode": episode,
            "quality": quality or None,
        })
    else:
        #----- Real: force the title's own id and let metadata() parse from each file.
        #----- An optional season is only used as a fallback for files that carry an
        #----- episode but no season (e.g. absolute-numbered anime).
        imdb_id = doc.get("imdb_id") or ""
        default_id = imdb_id if str(imdb_id).startswith("tt") else str(int(tmdb_id))

        season = payload.get("season")
        if media_type == "tv" and season is not None and str(season).strip() != "":
            try:
                season = int(season)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Season must be a number.")
        else:
            season = None

        session.update({
            "kind": "real",
            "default_id": default_id,
            "season": season,
            "episode": None,
            "quality": None,
        })

    Backend.MANUAL_SESSION = session
    return {"status": "success", "session": session}


#----- Clear the active manual upload session
async def clear_manual_session_api() -> dict:
    Backend.MANUAL_SESSION = None
    return {"status": "success"}


#----- Start a scan or rescan job over the given channels
async def start_scan_api(payload: dict) -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")

    mode = str(payload.get("mode", "scan")).lower()
    if mode not in ("scan", "rescan"):
        raise HTTPException(status_code=400, detail="mode must be 'scan' or 'rescan'.")
    channels = payload.get("channels") or []
    if not isinstance(channels, list):
        raise HTTPException(status_code=400, detail="'channels' must be a list.")

    result = await scan_manager.start(client, channels, mode=mode)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start scan."))
    return {"status": "success", **result}


async def cancel_scan_api() -> dict:
    result = await scan_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}


async def scan_status_api() -> dict:
    return {"status": "success", "data": scan_manager.get_status()}


async def start_dbcheck_api() -> dict:
    client = _scan_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No Telegram client is connected yet.")
    result = await dbcheck_manager.start(client)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start DB check."))
    return {"status": "success", **result}


async def cancel_dbcheck_api() -> dict:
    result = await dbcheck_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}


async def dbcheck_status_api() -> dict:
    return {"status": "success", "data": dbcheck_manager.get_status()}


#----- ── Duplicate check & cleanup ──
async def start_duplicate_check_api() -> dict:
    result = await duplicate_manager.start()
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message", "Could not start duplicate scan."))
    return {"status": "success", **result}


async def cancel_duplicate_check_api() -> dict:
    result = await duplicate_manager.cancel()
    return {"status": "success" if result.get("ok") else "error", **result}


async def duplicate_check_status_api() -> dict:
    return {"status": "success", "data": duplicate_manager.get_status()}


#----- Remove selected duplicate streams, or (delete_all) keep the newest per group
async def purge_duplicates_api(payload: dict | None = None) -> dict:
    payload = payload or {}
    delete_all = bool(payload.get("delete_all"))
    stream_ids = payload.get("stream_ids")
    if not delete_all and (not isinstance(stream_ids, list) or not stream_ids):
        raise HTTPException(status_code=400, detail="Provide 'stream_ids' or set 'delete_all'.")
    result = await duplicate_manager.purge(stream_ids, delete_all=delete_all)
    return {"status": "success" if result.get("ok") else "error", **result}


#----- Purge dead links (from last dbcheck, flagged in DB, or a specific set)
async def purge_dead_links_api(payload: dict | None = None) -> dict:
    payload = payload or {}
    source = str(payload.get("source", "dbcheck")).lower()
    stream_ids = payload.get("stream_ids")

    if stream_ids is not None:
        result = await dbcheck_manager.purge(stream_ids)
    elif source == "flagged":
        try:
            flagged = await db.get_all_dead_links()
            ids = list({d.get("quality_id") for d in flagged if d.get("quality_id")})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not load flagged dead links: {e}")
        result = await dbcheck_manager.purge(ids)
    else:
        result = await dbcheck_manager.purge()

    return {"status": "success" if result.get("ok") else "error", **result}



#----- ── System & Maintenance (web replacements for /stats, /log, /restart) ──

LOG_FILE = "log.txt"


#----- Aggregate content + system metrics across all storage DBs (was /stats)
async def get_db_stats_api() -> dict:
    try:
        total_movies = total_tv = total_episodes = total_streams = total_db_size = 0

        for i in range(1, db.current_db_index + 1):
            storage = db.dbs.get(f"storage_{i}")
            if storage is None:
                continue

            total_movies += await storage["movie"].count_documents({})
            async for movie in storage["movie"].find({}, {"telegram": 1}):
                total_streams += len(movie.get("telegram", []))

            total_tv += await storage["tv"].count_documents({})
            async for show in storage["tv"].find({}, {"seasons": 1}):
                for season in show.get("seasons", []):
                    for episode in season.get("episodes", []):
                        total_episodes += 1
                        total_streams += len(episode.get("telegram", []))

            try:
                total_db_size += (await storage.command("dbStats")).get("dataSize", 0)
            except Exception:
                pass

        return {
            "status": "success",
            "data": {
                "version": __version__,
                "movies": total_movies,
                "tv_shows": total_tv,
                "episodes": total_episodes,
                "streams": total_streams,
                "uptime": get_readable_time(int(time() - StartTime)),
                "db_size": get_readable_file_size(total_db_size),
                "storage_dbs": db.current_db_index,
                "auth_channels": len(SettingsManager.current().auth_channels),
            },
        }
    except Exception as e:
        LOGGER.error(f"[Stats] Error: {e}")
        return {"status": "error", "message": str(e)}


#----- First-run setup checklist: what's configured vs still missing
async def setup_status_api() -> dict:
    s = SettingsManager.current()
    checks = [
        {"key": "tmdb", "label": "TMDB API key", "done": bool(s.tmdb_api),
         "hint": "Powers automatic poster & metadata matching."},
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


#----- Config backup export (settings minus secrets + catalogs, plans, tokens)
async def export_config_api() -> dict:
    return await export_config()


#----- Config backup restore
async def import_config_api(payload: dict) -> dict:
    try:
        result = await import_config(payload)
        return {"status": "success", "result": result, "message": "Backup restored successfully."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOGGER.error(f"Config import error: {e}")
        return {"status": "error", "message": str(e)}


#----- Lightweight liveness probe; start_time changes on every boot (restart detection)
async def health_api() -> dict:
    return {"status": "ok", "start_time": StartTime, "version": __version__}


#----- Full diagnostics report (DBs, bot clients, TMDB, base URL)
async def health_report_api(force: bool = False) -> dict:
    try:
        return {"status": "success", "data": await run_health_checks(force=force)}
    except Exception as e:
        LOGGER.error(f"Health report error: {e}")
        return {"status": "error", "message": str(e)}


#----- Tail of the log file for the web viewer (was /log)
async def get_logs_api(lines: int = 300) -> dict:
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        return {"status": "error", "message": "Log file not found.", "log": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-max(1, min(lines, 2000)):]
        return {"status": "success", "log": "".join(tail)}
    except Exception as e:
        return {"status": "error", "message": str(e), "log": ""}


#----- Download the raw log file (was /log document)
async def download_logs_api():
    path = os.path.abspath(LOG_FILE)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log file not found.")
    return FileResponse(path, filename="log.txt", media_type="text/plain")


#----- Run the updater then re-exec the app; runs after the HTTP response is flushed
async def _perform_restart(delay: float = 1.0) -> None:
    await asyncio.sleep(delay)
    try:
        LOGGER.info("Web-triggered restart: running updater...")
        proc = await asyncio.create_subprocess_exec("uv", "run", "update.py")
        await proc.wait()
    except Exception as e:
        LOGGER.error(f"Restart updater failed: {e}")

    uv_path = shutil.which("uv")
    if not uv_path:
        LOGGER.error("Restart aborted: uv not found in PATH.")
        return
    LOGGER.info("Web-triggered restart: re-executing app...")
    os.execl(uv_path, uv_path, "run", "-m", "Backend")


#----- Trigger a restart from the web (was /restart)
async def restart_app_api() -> dict:
    asyncio.create_task(_perform_restart())
    return {"status": "success", "message": "Restart initiated — the server will be back shortly."}
