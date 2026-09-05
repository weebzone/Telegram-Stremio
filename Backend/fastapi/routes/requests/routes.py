from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from Backend.fastapi.security.credentials import require_auth
from Backend.helper.requests.manager import (
    delete_request,
    list_requests,
    popular_pending,
    search_titles,
    set_status,
    submit_request,
)
from Backend.logger import LOGGER

router = APIRouter(tags=["requests"])


@router.get("/api/request/search")
async def request_search(q: str = Query("")):
    try:
        return {"status": "success", "data": await search_titles(q)}
    except Exception as e:
        LOGGER.error(f"Request search error: {e}")
        return {"status": "error", "message": str(e), "data": []}


@router.get("/api/request/popular")
async def request_popular():
    try:
        return {"status": "success", "data": await popular_pending()}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}


@router.post("/api/request/submit")
async def request_submit(payload: dict, request: Request):
    client_ip = request.client.host if request.client else None
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


@router.get("/api/admin/requests")
async def get_requests(_: bool = Depends(require_auth)):
    try:
        return {"status": "success", "data": await list_requests()}
    except Exception as e:
        LOGGER.error(f"Requests API error: {e}")
        return {"status": "error", "message": str(e)}


@router.patch("/api/admin/requests/{request_id}")
async def update_request(request_id: str, payload: dict, _: bool = Depends(require_auth)):
    new_status = str(payload.get("status", "")).strip()
    doc = await set_status(request_id, new_status)
    if not doc:
        raise HTTPException(status_code=404, detail="Request not found or invalid status.")
    return {"status": "success", "data": doc}


@router.delete("/api/admin/requests/{request_id}")
async def delete_request_route(request_id: str, _: bool = Depends(require_auth)):
    if not await delete_request(request_id):
        raise HTTPException(status_code=404, detail="Request not found.")
    return {"status": "success", "message": "Request deleted."}
