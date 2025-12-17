from fastapi import Request, Form, HTTPException, Depends
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from Backend.fastapi.security.credentials import verify_credentials, require_auth, is_authenticated, get_current_user
from Backend.fastapi.themes import get_theme, get_all_themes
from Backend import db
from Backend.pyrofork.bot import work_loads, multi_clients, StreamBot
from Backend.helper.pyro import get_readable_time
from Backend import StartTime, __version__
from time import time


templates = Jinja2Templates(directory="Backend/fastapi/templates")

async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    
    return templates.TemplateResponse("login.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name
    })

async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["authenticated"] = True
        request.session["username"] = username
        return RedirectResponse(url="/", status_code=302)
    else:
        theme_name = request.session.get("theme", "purple_gradient")
        theme = get_theme(theme_name)
        return templates.TemplateResponse("login.html", {
            "request": request,
            "theme": theme,
            "themes": get_all_themes(),
            "current_theme": theme_name,
            "error": "Invalid credentials"
        })

async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

async def set_theme(request: Request, theme: str = Form(...)):
    if theme in get_all_themes():
        request.session["theme"] = theme
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=302)

async def dashboard_page(request: Request, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    
    try:
        db_stats = await db.get_database_stats()
        total_movies = sum(stat.get("movie_count", 0) for stat in db_stats)
        total_tv_shows = sum(stat.get("tv_count", 0) for stat in db_stats)
        
        system_stats = {
            "server_status": "running",
            "uptime": get_readable_time(time() - StartTime),
            "telegram_bot": f"@{StreamBot.username}" if StreamBot and StreamBot.username else "@StreamBot",
            "connected_bots": len(multi_clients),
            "loads": {
                f"bot{c + 1}": l
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            } if work_loads else {},
            "version": __version__,
            "movies": total_movies,
            "tv_shows": total_tv_shows,
            "databases": db_stats,
            "total_databases": len(db_stats),
            "current_db_index": db.current_db_index
        }
    except Exception as e:
        print(f"Dashboard error: {e}")
        system_stats = {
            "server_status": "error", 
            "error": str(e),
            "uptime": "N/A",
            "telegram_bot": "@StreamBot",
            "connected_bots": 0,
            "loads": {},
            "version": "1.0.0",
            "movies": 0,
            "tv_shows": 0,
            "databases": [],
            "total_databases": 0,
            "current_db_index": 1
        }
    

    api_tokens = await db.get_all_api_tokens()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "current_user": current_user,
        "system_stats": system_stats,
        "api_tokens": api_tokens
    })
    

async def media_management_page(request: Request, media_type: str = "movie", _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    
    return templates.TemplateResponse("media_management.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "current_user": current_user,
        "media_type": media_type
    })

async def edit_media_page(request: Request, tmdb_id: int, db_index: int, media_type: str, _: bool = Depends(require_auth)):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    current_user = get_current_user(request)
    
    try:
        media_details = await db.get_document(media_type, tmdb_id, db_index)
        if not media_details:
            raise HTTPException(status_code=404, detail="Media not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    return templates.TemplateResponse("media_edit.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "current_user": current_user,
        "tmdb_id": tmdb_id,
        "db_index": db_index,
        "media_type": media_type,
        "media_details": media_details
    })

async def public_status_page(request: Request):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    
    try:
        db_stats = await db.get_database_stats()
        total_movies = sum(stat.get("movie_count", 0) for stat in db_stats)
        total_tv_shows = sum(stat.get("tv_count", 0) for stat in db_stats)
        
        public_stats = {
            "status": "operational",
            "uptime": "99.9%",
            "total_content": total_movies + total_tv_shows,
            "databases_online": len(db_stats)
        }
    except Exception:
        public_stats = {
            "status": "maintenance",
            "uptime": "N/A",
            "total_content": 0,
            "databases_online": 0
        }
    
    return templates.TemplateResponse("public_status.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "stats": public_stats,
        "is_authenticated": is_authenticated(request)
    })

async def stremio_guide_page(request: Request):
    theme_name = request.session.get("theme", "purple_gradient")
    theme = get_theme(theme_name)
    
    return templates.TemplateResponse("stremio_guide.html", {
        "request": request,
        "theme": theme,
        "themes": get_all_themes(),
        "current_theme": theme_name,
        "is_authenticated": is_authenticated(request)
    })
