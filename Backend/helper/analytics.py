import time
from datetime import datetime

import httpx

from Backend import db
from Backend.helper.custom_dl import ACTIVE_STREAMS
from Backend.logger import LOGGER

_IP_CACHE = {}
_IP_TTL = 6 * 3600
_LAST_FULL = {}
_FULL_INTERVAL = 60
ONLINE_WINDOW = 120

_APP_MAP = [
    ("nuvio", "Nuvio"),
    ("stremio", "Stremio"),
    ("vlc", "VLC"),
    ("infuse", "Infuse"),
    ("outplayer", "Outplayer"),
    ("mpv", "mpv"),
    ("kodi", "Kodi"),
    ("exoplayer", "ExoPlayer"),
    ("android tv", "Android TV"),
    ("smarttv", "Smart TV"),
    ("okhttp", "Android App"),
    ("cfnetwork", "iOS App"),
    ("mozilla", "Browser"),
]


def client_ip_from(request) -> str:
    xff = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def parse_app(user_agent: str) -> str:
    if not user_agent:
        return "Unknown"
    low = user_agent.lower()
    for needle, name in _APP_MAP:
        if needle in low:
            return name
    return "Unknown"


async def lookup_ip(ip: str) -> dict:
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.")) or ip in ("::1", "localhost"):
        return {"country": "Local", "city": "", "isp": "", "proxy": False}
    now = time.time()
    cached = _IP_CACHE.get(ip)
    if cached and now - cached[1] < _IP_TTL:
        return cached[0]
    data = {"country": "", "city": "", "isp": "", "proxy": False}
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,city,isp,proxy,hosting"},
            )
            if resp.status_code == 200:
                j = resp.json()
                if j.get("status") == "success":
                    data = {
                        "country": j.get("country") or "",
                        "city": j.get("city") or "",
                        "isp": j.get("isp") or "",
                        "proxy": bool(j.get("proxy") or j.get("hosting")),
                    }
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] IP lookup failed for {ip}: {e}")
    _IP_CACHE[ip] = (data, now)
    return data


async def record_stream_start(token: str, name: str, ip: str, user_agent: str) -> None:
    if not token:
        return
    try:
        await db.dbs["tracking"]["user_activity"].update_one(
            {"_id": token},
            {"$set": {"last_active": datetime.utcnow()}, "$setOnInsert": {"name": name or "Unknown"}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] activity ping failed: {e}")
        return

    now_ts = time.time()
    if now_ts - _LAST_FULL.get(token, 0) < _FULL_INTERVAL:
        return
    _LAST_FULL[token] = now_ts

    geo = await lookup_ip(ip)
    doc = {
        "name": name or "Unknown",
        "ip": ip or "",
        "app": parse_app(user_agent),
        "user_agent": user_agent or "",
        "country": geo.get("country"),
        "city": geo.get("city"),
        "isp": geo.get("isp"),
        "proxy": geo.get("proxy", False),
        "last_active": datetime.utcnow(),
    }
    try:
        await db.dbs["tracking"]["user_activity"].update_one({"_id": token}, {"$set": doc}, upsert=True)
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] record failed: {e}")


async def get_activity_overview() -> dict:
    now = datetime.utcnow()
    playing = {}
    for info in ACTIVE_STREAMS.values():
        meta = info.get("meta", {}) or {}
        tok = meta.get("token")
        if tok:
            playing[tok] = meta.get("title") or "Streaming"

    try:
        docs = await db.dbs["tracking"]["user_activity"].find().sort("last_active", -1).to_list(None)
    except Exception:
        docs = []

    rows = []
    for d in docs:
        token = d.get("_id")
        last = d.get("last_active")
        online = token in playing
        if not online and last:
            try:
                online = (now - last).total_seconds() < ONLINE_WINDOW
            except Exception:
                online = False
        rows.append({
            "token": token,
            "name": d.get("name") or "Unknown",
            "online": online,
            "now_playing": playing.get(token),
            "last_title": d.get("last_title"),
            "ip": d.get("ip") or "",
            "country": d.get("country") or "",
            "city": d.get("city") or "",
            "isp": d.get("isp") or "",
            "app": d.get("app") or "Unknown",
            "proxy": bool(d.get("proxy")),
            "streams": int(d.get("streams") or 0),
            "last_active": last.isoformat() if last else None,
        })

    rows.sort(key=lambda r: 0 if r["online"] else 1)
    return {"users": rows, "online_count": sum(1 for r in rows if r["online"]), "total": len(rows)}
