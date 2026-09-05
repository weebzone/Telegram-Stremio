import asyncio

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from Backend import __version__
from Backend.fastapi.themes import DEFAULT_THEME, DEFAULT_STYLE, get_theme
from Backend.fastapi.routes.stream_routes import decay_client_failures
from Backend.fastapi.routes.stream_routes import router as stream_router
from Backend.fastapi.routes.stremio_routes import router as stremio_router
from Backend.fastapi.routes.webdav_routes import router as webdav_router
from Backend.fastapi.routes.tools import router as tools_router, tools_page
from Backend.fastapi.routes.media import router as media_router, media_management_page, edit_media_page
from Backend.fastapi.routes.dashboard import router as dashboard_router, dashboard_page, admin_dashboard_page
from Backend.fastapi.routes.settings import router as settings_router, settings_page
from Backend.fastapi.routes.catalogs import router as catalogs_router, custom_catalogs_page
from Backend.fastapi.routes.auth import login_page, login_post, logout, set_theme
from Backend.fastapi.routes.public import public_status_page, stremio_guide_page
from Backend.fastapi.routes.requests import router as requests_router, admin_requests_page, public_request_page
from Backend.fastapi.routes.access import router as access_router, admin_access_page
from Backend.fastapi.routes.subscriptions import router as subscriptions_router, admin_subscriptions_page
from Backend.fastapi.security.credentials import require_auth
from Backend.pyrofork.bot import work_loads_summary

templates = Jinja2Templates(directory="Backend/fastapi/templates")

app = FastAPI(
    title="Telegram Stremio Media Server",
    description="A powerful, self-hosted Telegram Stremio Media Server built with FastAPI, MongoDB, and PyroFork seamlessly integrated with Stremio for automated media streaming and discovery.",
    version=__version__
)

#----- Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    app.mount("/static", StaticFiles(directory="Backend/fastapi/static"), name="static")
except Exception:
    pass


@app.on_event("startup")
async def _startup():
    asyncio.create_task(decay_client_failures())


#----- Streaming and Stremio routers
app.include_router(stream_router)
app.include_router(tools_router)
app.include_router(media_router)
app.include_router(dashboard_router)
app.include_router(settings_router)
app.include_router(catalogs_router)
app.include_router(requests_router)
app.include_router(access_router)
app.include_router(subscriptions_router)
app.include_router(stremio_router)
app.include_router(webdav_router)


#----- Public routes (no authentication)
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    return await login_page(request)

@app.post("/login", response_class=HTMLResponse)
async def login_post_route(request: Request, username: str = Form(...), password: str = Form(...)):
    return await login_post(request, username, password)

@app.get("/logout")
async def logout_route(request: Request):
    return await logout(request)

@app.post("/set-theme")
async def set_theme_route(request: Request, theme: str = Form(None), style: str = Form(None)):
    return await set_theme(request, theme, style)

@app.get("/manifest.webmanifest")
async def pwa_manifest(request: Request):
    theme_name = request.session.get("theme", DEFAULT_THEME)
    style_name = request.session.get("style", DEFAULT_STYLE)
    theme = get_theme(theme_name, style_name)
    return JSONResponse(
        {
            "name": "Telegram Stremio",
            "short_name": "TG Stremio",
            "description": "Telegram Stremio media management",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": theme["colors"]["background"],
            "theme_color": theme["colors"]["primary"],
            "icons": [
                {
                    "src": "/pwa-icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any"
                },
                {
                    "src": "/pwa-icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "maskable"
                }
            ]
        },
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"}
    )

@app.get("/pwa-icon.svg")
async def pwa_icon(request: Request):
    theme_name = request.session.get("theme", DEFAULT_THEME)
    style_name = request.session.get("style", DEFAULT_STYLE)
    theme = get_theme(theme_name, style_name)
    primary = theme["colors"]["primary"]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        f'<rect width="512" height="512" rx="96" fill="{primary}"/>'
        f'<path d="M200 152l176 104-176 104z" fill="white"/>'
        f'</svg>'
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"}
    )

@app.get("/sw.js")
async def service_worker():
    js = (
        "self.addEventListener('install',e=>self.skipWaiting());"
        "self.addEventListener('activate',e=>e.waitUntil(clients.claim()));"
        "self.addEventListener('fetch',e=>e.respondWith(fetch(e.request).catch(()=>caches.match(e.request))));"
    )
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"}
    )

@app.get("/status", response_class=HTMLResponse)
async def public_status(request: Request):
    return await public_status_page(request)

@app.get("/stremio", response_class=HTMLResponse)
async def stremio_guide(request: Request):
    return await stremio_guide_page(request)

@app.get("/open/{app_name}/{media_type}/{content_id}", response_class=HTMLResponse)
async def open_in_app(app_name: str, media_type: str, content_id: str):
    stremio_type = "series" if media_type in ("series", "tv") else "movie"
    web = f"https://web.stremio.com/#/detail/{stremio_type}/{content_id}/{content_id}"
    schemes = {
        "nuvio": f"nuvio://meta?type={stremio_type}&id={content_id}",
        "stremio": f"stremio:///detail/{stremio_type}/{content_id}",
    }
    scheme = schemes.get(app_name, schemes["stremio"])
    label = "Nuvio" if app_name == "nuvio" else "Stremio"
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Opening {label}…</title>
<style>body{{font-family:system-ui,-apple-system,sans-serif;background:#0f172a;color:#f8fafc;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;text-align:center}}a{{color:#60a5fa}}.b{{display:inline-block;margin-top:16px;padding:12px 22px;background:#3b82f6;color:#fff;border-radius:12px;text-decoration:none;font-weight:700}}</style>
</head><body><div><h2>Opening in {label}…</h2>
<p>If nothing happens, use the buttons below.</p>
<a class="b" href="{scheme}">Open {label}</a><br>
<a class="b" style="background:#334155" href="{web}">Open Stremio Web</a></div>
<script>setTimeout(function(){{window.location.href="{scheme}";}},200);</script>
</body></html>"""
    return HTMLResponse(html)


#----- Protected routes (authentication required)
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, _: bool = Depends(require_auth)):
    return await dashboard_page(request, _)

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, _: bool = Depends(require_auth)):
    return await admin_dashboard_page(request, _)

@app.get("/media/manage", response_class=HTMLResponse)
async def media_management(request: Request, media_type: str = "movie", custom: bool = False, _: bool = Depends(require_auth)):
    return await media_management_page(request, media_type, custom, _)

@app.get("/catalogs", response_class=HTMLResponse)
async def custom_catalogs(request: Request, _: bool = Depends(require_auth)):
    return await custom_catalogs_page(request, _)

@app.get("/media/edit", response_class=HTMLResponse)
async def edit_media(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    return await edit_media_page(request, tmdb_id, db_index, media_type, _)















@app.get("/admin/access", response_class=HTMLResponse)
async def admin_access(request: Request, _: bool = Depends(require_auth)):
    return await admin_access_page(request, _)

@app.get("/admin/subscriptions", response_class=HTMLResponse)
async def admin_subscriptions(request: Request, _: bool = Depends(require_auth)):
    return await admin_subscriptions_page(request, _)

@app.get("/admin/requests", response_class=HTMLResponse)
async def admin_requests(request: Request, _: bool = Depends(require_auth)):
    return await admin_requests_page(request, _)

@app.get("/request", response_class=HTMLResponse)
async def public_request(request: Request):
    return await public_request_page(request)

@app.get("/admin/tools", response_class=HTMLResponse)
async def admin_tools(request: Request, _: bool = Depends(require_auth)):
    return await tools_page(request, _)

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request, _: bool = Depends(require_auth)):
    return await settings_page(request, _)

@app.exception_handler(401)
async def auth_exception_handler(request: Request, exc):
    # API / stream / WebDAV clients must receive a real 401, not an HTML login redirect.
    path = request.url.path or ""
    if path.startswith(("/webdav", "/dl/", "/sub/", "/stremio/", "/api/", "/thumb/")):
        from fastapi.responses import JSONResponse
        detail = getattr(exc, "detail", "Unauthorized")
        headers = {}
        # Preserve WWW-Authenticate for WebDAV Basic auth prompts
        if hasattr(exc, "headers") and exc.headers:
            headers.update(exc.headers)
        return JSONResponse(status_code=401, content={"detail": detail}, headers=headers)
    return RedirectResponse(url="/login", status_code=302)
