import asyncio
import json
from datetime import datetime
from time import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from Backend import StartTime, __version__, db
from Backend.fastapi.routes.stream_routes import _streamer_by_client
from Backend.fastapi.routes.stremio_routes import invalidate_membership_cache
from Backend.helper.auto_catalog import (
    get_auto_catalog_settings,
    get_auto_catalog_sync_status,
    start_auto_catalog_sync_background,
    update_auto_catalog_settings,
)
from Backend.helper.custom_dl import ByteStreamer, _speed_test_single_client, run_speed_test
from Backend.helper.encrypt import decode_string
from Backend.helper.metadata import (
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.passwords import hash_password
from Backend.helper.pyro import get_readable_time
from Backend.helper.scan_manager import dbcheck_manager, scan_manager
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import (
    StreamBot,
    client_avg_mbps,
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


#----- Media management
async def list_media_api(
    media_type: str = Query("movie", regex="^(movie|tv)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query("", max_length=100)
):
    try:
        if search:
            result = await db.search_documents(search, page, page_size)
            filtered_results = [item for item in result['results'] if item.get('media_type') == media_type]
            total_filtered = len(filtered_results)
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            paged_results = filtered_results[start_index:end_index]
            
            return {
                "total_count": total_filtered,
                "current_page": page,
                "total_pages": (total_filtered + page_size - 1) // page_size,
                "movies" if media_type == "movie" else "tv_shows": paged_results
            }
        else:
            if media_type == "movie":
                return await db.sort_movies([], page, page_size)
            else:
                return await db.sort_tv_shows([], page, page_size)
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
        daily_limit = payload.get("daily_limit_gb")
        monthly_limit = payload.get("monthly_limit_gb")

        if not token_name:
            raise HTTPException(status_code=400, detail="Token name is required")

        new_token = await db.add_api_token(
            token_name,
            _parse_limit(daily_limit),
            _parse_limit(monthly_limit)
        )
        return new_token
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
            "display_name": f"Bot {client_index + 1}",
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
        return {"status": "success", "data": users}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def manage_subscriber_api(user_id: int, payload: dict) -> dict:
    try:
        action = payload.get("action")
        days = int(payload.get("days", 0))
        
        if action not in ["extend", "reduce", "delete"]:
            raise HTTPException(status_code=400, detail="Invalid action")
            
        success = await db.manage_subscriber(user_id, action, days)

        #----- On revoke, kick the user from the group immediately (ban+unban)
        if success and action == "delete" and SettingsManager.current().subscription:
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
            verb = {"extend": "extended", "reduce": "reduced", "delete": "revoked"}.get(action, "updated")
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

        #----- Non-empty display name for a user
        def display_name(user, user_id, token_name=None):
            if user:
                n = user.get("first_name") or user.get("username")
                if n:
                    return n
            if token_name:
                return token_name
            return f"User {user_id}" if user_id else "Telegram User"

        #----- Unified access entry from optional user + token records
        def build_entry(user_id, user, token_doc):
            expiry = None
            sub_status = None
            user_found = bool(user)

            if user:
                sub_status = user.get("subscription_status")
                expiry = user.get("subscription_expiry")

            if token_doc:
                t_expiry = token_doc.get("subscription_expiry") or token_doc.get("expires_at")
                if t_expiry and not expiry:
                    expiry = t_expiry

            if SettingsManager.current().subscription:
                if not user_found:
                    is_expired = True
                elif sub_status != "active":
                    is_expired = True
                elif not expiry:
                    is_expired = True
                else:
                    is_expired = expiry < now
            else:
                is_expired = bool(expiry and expiry < now)

            token_str = token_doc.get("token") if token_doc else None
            created = token_doc.get("created_at") if token_doc else (user.get("created_at") if user else None)

            return {
                "token": token_str,
                "user_id": user_id,
                "user_name": display_name(user, user_id, token_doc.get("name") if token_doc else None),
                "user_found": user_found,
                "has_token": bool(token_str),
                "created_at": created.isoformat() if created else None,
                "expires_at": expiry.isoformat() if expiry else None,
                "is_expired": is_expired,
                "sub_status": sub_status,
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
        return {"tokens": result}
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
        if days < 1:
            raise HTTPException(status_code=400, detail="Days must be at least 1.")
        result = await db.assign_subscription(user_id, days)
        return {"status": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#----- Link an orphan token to a Telegram user_id
async def link_token_user_api(token: str, user_id: int) -> dict:
    try:
        success = await db.link_token_user(token, user_id)
        if success:
            return {"status": "success", "message": f"Token linked to user {user_id}."}
        raise HTTPException(status_code=404, detail="Token not found or already linked.")
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


async def create_custom_catalog_api(payload: dict):
    name = (payload.get("name") or "").strip()
    visible = bool(payload.get("visible", True))
    if not name:
        raise HTTPException(status_code=400, detail="Catalog name is required.")

    catalog_id = await db.create_custom_catalog(name=name, visible=visible)
    if not catalog_id:
        raise HTTPException(status_code=500, detail="Failed to create catalog.")

    catalog = await db.get_custom_catalog(catalog_id)
    return {"message": "Catalog created successfully.", "catalog": catalog}


async def update_custom_catalog_api(catalog_id: str, payload: dict):
    name = payload.get("name")
    visible = payload.get("visible") if "visible" in payload else None
    result = await db.update_custom_catalog(catalog_id, name=name, visible=visible)
    if not result:
        catalog = await db.get_custom_catalog(catalog_id)
        if not catalog:
            raise HTTPException(status_code=404, detail="Catalog not found.")
    return {"message": "Catalog updated successfully.", "catalog": await db.get_custom_catalog(catalog_id)}


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
    message = "Added to catalog." if added else "Already exists in this catalog."
    return {"message": message, "added": added}


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
    return {"message": "Removed from catalog.", "removed": True}


async def auto_sync_custom_catalogs_api(full_rebuild: bool = False):
    try:
        result = await start_auto_catalog_sync_background(db, force=True, full_rebuild=full_rebuild)
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
    bool_keys = {"replace_mode", "hide_catalog", "subscription", "show_proxy_and_non_proxy_both"}
    for key in bool_keys:
        if key in payload:
            payload[key] = bool(payload[key])

    list_str_keys = {"auth_channels", "multi_tokens", "extra_databases", "global_search_channels", "anime_channels"}
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

    #----- Strip whitespace from string fields
    for key in ("tmdb_api", "base_url", "upstream_repo", "upstream_branch",
                "admin_username", "admin_password", "session_secret", "http_proxy_url",
                "payment_instructions", "payment_qr_url"):
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
