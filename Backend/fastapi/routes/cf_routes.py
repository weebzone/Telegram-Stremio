import json

from fastapi import APIRouter, HTTPException, Request

from Backend import db
from Backend.config import Telegram
from Backend.helper.analytics import record_stream_start
from Backend.helper.cf_live import apply_report
from Backend.helper.cf_stream import verify_worker_request
from Backend.helper.own_bots import worker_tokens
from Backend.helper.session_auth import get_active_session_string
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

router = APIRouter(tags=["Cloudflare"])


#----- Only the Cloudflare Worker (holding the shared secret) may call these routes
async def _verified_body(request: Request) -> tuple:
    body = await request.body()
    signed = (request.method, request.url.path, body, request.headers.get("x-cf-time", ""), request.headers.get("x-cf-sig", ""))
    if verify_worker_request(*signed):
        return body, None
    for doc in await worker_tokens():
        if verify_worker_request(*signed, doc["cf"]["secret"]):
            return body, doc
    raise HTTPException(status_code=401, detail="Bad signature")


#----- Telegram credentials the Worker needs to stream: API app, bot pool, optional userbot
@router.get("/api/cf/config")
async def cf_config(request: Request):
    _, owner = await _verified_body(request)
    if owner:
        bots = [b["token"] for b in (owner.get("own_bots") or {}).get("bots") or []]
        return {"api_id": Telegram.API_ID, "api_hash": Telegram.API_HASH, "bots": bots, "user_session": ""}
    tokens = [Telegram.BOT_TOKEN, *SettingsManager.current().multi_tokens]
    try:
        user_session = await get_active_session_string() or ""
    except Exception as e:
        LOGGER.warning(f"[CF] Could not load userbot session: {e}")
        user_session = ""
    return {
        "api_id": Telegram.API_ID,
        "api_hash": Telegram.API_HASH,
        "bots": [t for t in dict.fromkeys(tokens) if t],
        "user_session": user_session,
    }


#----- Bytes streamed per token (for daily/monthly limits) and stream-start analytics
@router.post("/api/cf/usage")
async def cf_usage(request: Request):
    body, _ = await _verified_body(request)
    data = json.loads(body or b"{}")
    for token, delta in (data.get("usage") or {}).items():
        if isinstance(delta, int) and delta > 0:
            try:
                await db.update_token_usage(token, delta)
            except Exception as e:
                LOGGER.error(f"[CF] Usage update failed for token: {e}")
    names = {}
    for start in data.get("starts") or []:
        token = str(start.get("token") or "")
        if not token:
            continue
        if token not in names:
            token_data = await db.get_api_token(token)
            names[token] = token_data.get("name") if token_data else None
        await record_stream_start(token, names[token], str(start.get("ip") or ""), str(start.get("ua") or ""))
    #----- Live streams, so dashboards and user activity show Cloudflare streams too
    if isinstance(data.get("streams"), list) and data.get("member"):
        try:
            await apply_report(str(data["member"]), data.get("client_index"), data.get("dc"), data["streams"])
        except Exception as e:
            LOGGER.error(f"[CF] Live stream report failed: {e}")
    return {"ok": True}
