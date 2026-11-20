"""Central role enforcement for every HTTP request.

SessionMiddleware must sit OUTSIDE this middleware so `request.session` is
already populated. That is why `__main__.py` adds AuthorizationMiddleware
first and SessionMiddleware second (last added = outermost).
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from Backend.fastapi.security.permissions import (
    ROLE_PERMISSIONS,
    has_permission,
    permissions_for,
    required_permission,
)
from Backend.logger import LOGGER


class AuthorizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/static/"):
            return await call_next(request)

        try:
            session = request.session
        except AssertionError:
            # SessionMiddleware not installed (e.g. imported without __main__).
            return await call_next(request)

        if not session.get("authenticated"):
            return await call_next(request)

        role = await self._resolve_role(session.get("username") or "")
        request.state.role = role
        request.state.permissions = permissions_for(role)

        # Account deleted or disabled after the session was issued.
        # /logout and /login must stay reachable or the redirect below loops.
        if role is None and path not in ("/logout", "/login"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
            return RedirectResponse("/logout", status_code=302)

        permission = required_permission(request.method, path)
        if permission and not has_permission(role, permission):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
            return RedirectResponse("/", status_code=302)

        return await call_next(request)

    @staticmethod
    async def _resolve_role(username: str):
        if not username:
            return None
        try:
            from Backend.helper import admin_users

            admin = await admin_users.get_admin(username)
        except Exception as e:
            LOGGER.error(f"[Authz] role lookup failed: {e}")
            return None

        if admin:
            if not admin.get("active", True):
                return None
            role = admin.get("role") or "admin"
            return role if role in ROLE_PERMISSIONS else "admin"

        # Legacy single owner kept in settings (never locks the owner out).
        try:
            from Backend.helper.settings_manager import SettingsManager

            if username == SettingsManager.current().admin_username:
                return "owner"
        except Exception as e:
            LOGGER.error(f"[Authz] settings owner fallback failed: {e}")
        return None
