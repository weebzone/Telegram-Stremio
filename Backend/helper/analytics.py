import time
from datetime import datetime, timedelta

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
    ("iina", "IINA"),
    ("potplayer", "PotPlayer"),
    ("mxplayer", "MX Player"),
    ("mxtech", "MX Player"),
    ("vimu", "Vimu"),
    ("justplayer", "Just Player"),
    ("splayer", "SPlayer"),
    ("kodi", "Kodi"),
    ("xbmc", "Kodi"),
    ("crkey", "Chromecast"),
    ("mpv", "mpv"),
    ("exoplayer", "ExoPlayer"),
    ("media3", "ExoPlayer"),
    ("applecoremedia", "Apple Player"),
    ("lavf", "FFmpeg"),
    ("ffmpeg", "FFmpeg"),
    ("libav", "FFmpeg"),
    ("edg", "Edge"),
    ("opr", "Opera"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("safari", "Safari"),
    ("okhttp", "Android App"),
    ("dalvik", "Android App"),
    ("cfnetwork", "iOS App"),
    ("mozilla", "Browser"),
]

_DEVICE_MAP = [
    ("android tv", "Android TV"),
    ("googletv", "Android TV"),
    ("bravia", "Android TV"),
    ("aft", "Fire TV"),
    ("appletv", "Apple TV"),
    ("apple tv", "Apple TV"),
    ("tvos", "Apple TV"),
    ("tizen", "Samsung TV"),
    ("web0s", "LG TV"),
    ("webos", "LG TV"),
    ("roku", "Roku"),
    ("smarttv", "Smart TV"),
    ("smart-tv", "Smart TV"),
    ("ipad", "iPad"),
    ("iphone", "iPhone"),
    ("ipod", "iPhone"),
    ("android", "Android"),
    ("windows nt", "Windows"),
    ("macintosh", "macOS"),
    ("mac os x", "macOS"),
    ("cros", "ChromeOS"),
    ("linux", "Linux"),
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


def parse_device(user_agent: str) -> str:
    if not user_agent:
        return ""
    low = user_agent.lower()
    for needle, name in _DEVICE_MAP:
        if needle in low:
            return name
    return ""


async def lookup_ip(ip: str) -> dict:
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.")) or ip in ("::1", "localhost"):
        return {"country": "Local", "city": "", "isp": "", "proxy": False, "mobile": False}
    now = time.time()
    cached = _IP_CACHE.get(ip)
    if cached and now - cached[1] < _IP_TTL:
        return cached[0]
    data = {"country": "", "city": "", "isp": "", "proxy": False, "mobile": False}
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,city,isp,proxy,hosting,mobile"},
            )
            if resp.status_code == 200:
                j = resp.json()
                if j.get("status") == "success":
                    data = {
                        "country": j.get("country") or "",
                        "city": j.get("city") or "",
                        "isp": j.get("isp") or "",
                        "proxy": bool(j.get("proxy") or j.get("hosting")),
                        "mobile": bool(j.get("mobile")),
                    }
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] IP lookup failed for {ip}: {e}")
    _IP_CACHE[ip] = (data, now)
    return data


async def record_stream_start(token: str, name: str, ip: str, user_agent: str) -> None:
    if not token:
        return
    coll = db.dbs["tracking"]["user_activity"]
    device = parse_device(user_agent)

    #----- Always refresh the cheap, device-identifying fields (no network) so the
    #----- most recent device/app is shown immediately.
    base = {
        "last_active": datetime.utcnow(),
        "ip": ip or "",
        "app": parse_app(user_agent),
        "device": device,
        "user_agent": user_agent or "",
    }
    try:
        await coll.update_one(
            {"_id": token},
            {"$set": base, "$setOnInsert": {"name": name or "Unknown"}},
            upsert=True,
        )
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] activity ping failed: {e}")
        return

    #----- The IP geo/ISP/VPN lookup is the only slow part — throttle it per token.
    now_ts = time.time()
    if now_ts - _LAST_FULL.get(token, 0) < _FULL_INTERVAL:
        return
    _LAST_FULL[token] = now_ts

    geo = await lookup_ip(ip)
    upd = {
        "country": geo.get("country"),
        "city": geo.get("city"),
        "isp": geo.get("isp"),
        "proxy": geo.get("proxy", False),
    }
    if not device and geo.get("mobile"):
        upd["device"] = "Mobile"
    try:
        await coll.update_one({"_id": token}, {"$set": upd})
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] geo update failed: {e}")


async def get_activity_overview(page: int = 1, per_page: int = 12) -> dict:
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=ONLINE_WINDOW)
    coll = db.dbs["tracking"]["user_activity"]

    playing = {}
    for info in ACTIVE_STREAMS.values():
        meta = info.get("meta", {}) or {}
        tok = meta.get("token")
        if tok:
            playing[tok] = meta.get("title") or "Streaming"

    try:
        total = await coll.count_documents({})
        online_count = await coll.count_documents({"last_active": {"$gte": cutoff}})
    except Exception:
        total, online_count = 0, 0
    online_count = max(online_count, len(playing))

    per_page = max(1, min(int(per_page or 12), 60))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page or 1), total_pages))
    offset = (page - 1) * per_page

    try:
        docs = await coll.find().sort("last_active", -1).skip(offset).limit(per_page).to_list(per_page)
    except Exception:
        docs = []

    rows = []
    for d in docs:
        token = d.get("_id")
        last = d.get("last_active")
        online = token in playing
        if not online and last:
            try:
                online = last >= cutoff
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
            "device": d.get("device") or "",
            "proxy": bool(d.get("proxy")),
            "streams": int(d.get("streams") or 0),
            "last_active": last.isoformat() if last else None,
        })

    rows.sort(key=lambda r: 0 if r["online"] else 1)
    return {
        "users": rows,
        "online_count": online_count,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }
