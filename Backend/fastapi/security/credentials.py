from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED

from Backend.helper.passwords import verify_password
from Backend.helper.settings_manager import SettingsManager


def verify_credentials(username: str, password: str) -> bool:
    s = SettingsManager.current()
    return username == s.admin_username and verify_password(password, s.admin_password)


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def get_current_user(request: Request) -> str | None:
    if is_authenticated(request):
        return request.session.get("username", "admin")
    return None


async def require_auth(request: Request) -> bool:
    if not is_authenticated(request):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return True
