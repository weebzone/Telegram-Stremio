from fastapi import HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED

from Backend.helper.passwords import verify_password
from Backend.helper.settings_manager import SettingsManager


#----- Match a username/password pair against the stored admin credentials.
#----- Checks the admin_users collection first; falls back to the single
#----- settings-based owner so access is NEVER lost (even if admin_users is
#----- empty or a migration failed).
async def verify_credentials(username: str, password: str) -> bool:
    from Backend.helper import admin_users

    admin = await admin_users.get_admin(username)
    if admin and admin.get("active", True):
        if verify_password(password, admin.get("password_hash", "")):
            return True
    #----- Fallback: the legacy single owner from settings.
    s = SettingsManager.current()
    if username == s.admin_username and verify_password(password, s.admin_password):
        return True
    return False


#----- Whether the session carries a valid authentication flag
def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


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


#----- Role of the currently logged-in admin (None if not authed).
async def get_current_role(request: Request) -> str | None:
    username = get_current_user(request)
    if not username:
        return None
    from Backend.helper import admin_users
    admin = await admin_users.get_admin(username)
    if not admin:
        #----- Legacy settings owner has full owner rights.
        if username == SettingsManager.current().admin_username:
            return "owner"
        return None
    return admin.get("role", "admin")


#----- Dependency: require the "owner" role. Guards security/deploy endpoints
#----- and admin-management operations. (FastAPI dependency)
async def require_owner(request: Request) -> bool:
    actual = await get_current_role(request)
    if actual != "owner":
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Requires owner role",
        )
    return True


#----- Verify an admin API token (for scripts / automation). Returns the admin
#----- doc if valid, else None.
async def verify_admin_token(token: str):
    if not token:
        return None
    from Backend.helper import admin_users
    return await admin_users.get_admin_by_token(token)
