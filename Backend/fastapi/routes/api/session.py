"""
api/session.py — Telegram userbot login session (phone code / 2FA).
"""


from fastapi import HTTPException

from Backend.helper.security.session_auth import (
    disconnect_session,
    get_session_status,
    reconnect_session,
    remove_session,
    start_login,
    submit_code,
    submit_password,
)


async def session_send_code_api(payload: dict):
    try:
        return await start_login(payload.get("phone"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def session_verify_code_api(payload: dict):
    try:
        return await submit_code(payload.get("login_id"), payload.get("code"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def session_verify_password_api(payload: dict):
    try:
        return await submit_password(payload.get("login_id"), payload.get("password"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def session_status_api():
    return await get_session_status()

async def session_disconnect_api():
    return await disconnect_session()

async def session_reconnect_api():
    try:
        return await reconnect_session()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

async def session_remove_api():
    return await remove_session()
