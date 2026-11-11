from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED

from Backend.helper.bounded import BoundedDict
from Backend.helper.passwords import verify_password
from Backend.helper.settings_manager import SettingsManager


#----- Match a username/password pair against the stored admin credentials
def verify_credentials(username: str, password: str) -> bool:
    s = SettingsManager.current()
    return username == s.admin_username and verify_password(password, s.admin_password)


#----- Whether the session carries a valid authentication flag
_revoked_sids = BoundedDict(1000)


def is_authenticated(request: Request) -> bool:
    if not request.session.get("authenticated"):
        return False
    if request.session.get("sid") in _revoked_sids:
        request.session.clear()
        return False
    return True


def revoke_session(request: Request) -> None:
    sid = request.session.get("sid")
    if sid:
        _revoked_sids[sid] = True
    request.session.clear()


#----- Logged-in username from the session, or None
def get_current_user(request: Request) -> str | None:
    if is_authenticated(request):
        return request.session.get("username", "admin")
    return None


#----- FastAPI dependency: raise 401 (redirected to /login) when unauthenticated
async def require_auth(request: Request) -> bool:
    if not is_authenticated(request):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return True
