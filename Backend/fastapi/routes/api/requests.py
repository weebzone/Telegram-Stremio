"""
api/requests.py — public media-request flow and admin request inbox.
"""


from fastapi import HTTPException

from Backend.helper.ops.requests_manager import (
    delete_request,
    list_requests,
    popular_pending,
    search_titles,
    set_status,
    submit_request,
)
from Backend.logger import LOGGER


async def request_search_api(q: str) -> dict:
    try:
        return {"status": "success", "data": await search_titles(q)}
    except Exception as e:
        LOGGER.error(f"Request search error: {e}")
        return {"status": "error", "message": str(e), "data": []}

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

async def request_popular_api() -> dict:
    try:
        return {"status": "success", "data": await popular_pending()}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}

async def get_requests_api() -> dict:
    try:
        return {"status": "success", "data": await list_requests()}
    except Exception as e:
        LOGGER.error(f"Requests API error: {e}")
        return {"status": "error", "message": str(e)}

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
