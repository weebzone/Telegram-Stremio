"""Role-based access control for the web panel.

Roles live in `admin_users.role` (Mongo, tracking DB):

    owner    - supreme admin, full access
    admin    - operational access (no admin management, no restart)
    uploader - content + failed-import repair, no settings/tools
    viewer   - read-only content access

Enforcement is centralised in AuthorizationMiddleware so every current and
future route is covered without repeating decorators on ~90 endpoints.
"""
from __future__ import annotations

from typing import Optional, Set

ROLE_PERMISSIONS: dict[str, Set[str]] = {
    "owner": {"*"},
    "admin": {
        "staff",
        "media.read",
        "media.write",
        "catalog.read",
        "catalog.write",
        "token.write",
        "sub.write",
        "request.write",
        "analytics.read",
        "tools.read",
        "tools.write",
        "imports.read",
        "imports.map",
        "settings.read",
        "settings.write",
    },
    "uploader": {
        "media.read",
        "media.write",
        "catalog.read",
        "imports.read",
        "imports.map",
        "analytics.read",
    },
    "viewer": {"media.read", "catalog.read", "analytics.read"},
}

VALID_ROLES = ("owner", "admin", "uploader", "viewer")

# Permissions that no other role has; expanded for the owner's UI context.
OWNER_ONLY_PERMISSIONS = {"admin.manage", "system.restart", "system.write"}

# Roles that can be handed out through an invite code (owner cannot).
INVITE_ROLES = ("admin", "uploader", "viewer")

# Settings keys only the owner may change, even though sub-admins can save the
# operational cards on the same page. Stripped server-side before persisting.
OWNER_ONLY_SETTINGS = {
    "admin_username",
    "admin_password",
    "session_secret",
    "extra_databases",
    "multi_tokens",
    "tmdb_api",
    "tvdb_api",
    "base_url",
    "upstream_repo",
    "upstream_branch",
    "cf_stream_url",
    "cf_stream_secret",
    "cf_stream_mode",
    "external_api_url",
    "external_api_token",
}

# No role check at all: public pages, token-authenticated media and docs.
_PUBLIC_EXACT = {
    "/login",
    "/logout",
    "/status",
    "/request",
    "/set-theme",
    "/manifest.webmanifest",
    "/pwa-icon.svg",
    "/sw.js",
    "/api/admin/register",
    "/docs",
    "/redoc",
    "/openapi.json",
}

_PUBLIC_PREFIXES = (
    "/static/",
    "/stremio/",
    "/dl/",
    "/sub/",
    "/thumb/",
    "/webdav/",
    "/api/cf/",
    "/stream/",
    "/open/",
    "/api/request/",
)


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


def permissions_for(role: Optional[str]) -> Set[str]:
    perms = ROLE_PERMISSIONS.get(role or "")
    if not perms:
        return set()
    if "*" in perms:
        expanded: Set[str] = set(OWNER_ONLY_PERMISSIONS)
        for role_perms in ROLE_PERMISSIONS.values():
            expanded.update(p for p in role_perms if p != "*")
        return expanded
    return set(perms)


def has_permission(role: Optional[str], permission: str) -> bool:
    perms = ROLE_PERMISSIONS.get(role or "")
    if not perms:
        return False
    return "*" in perms or permission in perms


def required_permission(method: str, path: str) -> Optional[str]:
    """Permission needed for a request, or None when no role check applies."""
    if is_public_path(path):
        return None

    p = path.rstrip("/") or "/"
    read = method.upper() in ("GET", "HEAD", "OPTIONS")

    #----- Always available to any authenticated account
    if p in ("/", "/status"):
        return None
    if p.startswith("/api/admin/me"):
        return None
    if p.startswith("/api/admin/version") or p.startswith("/api/admin/setup-status"):
        return None

    #----- Admin management (owner only)
    if p.startswith("/api/admin/admins") or p.startswith("/api/admin/invite"):
        return "admin.manage"

    #----- Security / deploy
    if p.startswith("/api/admin/settings/session"):
        return "settings.read" if read else "system.write"
    if p.startswith("/api/admin/settings"):
        return "settings.read" if read else "settings.write"
    if p.startswith("/api/admin/backup/export") or p.startswith("/api/admin/logs"):
        return "settings.read"
    if p.startswith("/api/admin/backup/import"):
        return "system.write"
    if p.startswith("/api/admin/restart"):
        return "system.restart"
    if p.startswith("/api/admin/clear-cache") or p.startswith("/api/admin/clear-analytics"):
        return "system.write"

    #----- Tools (global scan/dbcheck/duplicates) - never for uploaders
    if p.startswith("/api/admin/tools") or p == "/admin/tools":
        return "tools.read" if read else "tools.write"

    #----- Failed imports
    if p.startswith("/api/admin/imports") or p == "/admin/imports":
        return "imports.read" if read else "imports.map"

    #----- Analytics / system telemetry
    if (
        p.startswith("/api/admin/stream-analytics")
        or p.startswith("/api/admin/user-activity")
        or p.startswith("/api/admin/dead-links")
        or p.startswith("/api/admin/stats")
        or p.startswith("/api/admin/system-stats")
        or p.startswith("/api/admin/health")
        or p.startswith("/api/system/")
    ):
        return "analytics.read"

    #----- Content
    if p.startswith("/api/media") or p.startswith("/media/"):
        return "media.read" if read else "media.write"
    if p.startswith("/api/custom-catalogs") or p == "/catalogs":
        return "catalog.read" if read else "catalog.write"

    #----- Access control / billing / requests
    if p.startswith("/api/tokens") or p.startswith("/api/admin/access") or p == "/admin/access":
        return "token.write"
    if p.startswith("/api/admin/subscriptions") or p == "/admin/subscriptions":
        return "sub.write"
    if p.startswith("/api/admin/requests") or p == "/admin/requests":
        return "request.write"

    #----- Admin-only pages, and a safe default for anything unmapped
    if p == "/admin/dashboard":
        return "staff"
    if p.startswith("/api/") or p.startswith("/admin/"):
        return "staff"
    return None
