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

#----- App/device parsed from the ADDON-PROTOCOL User-Agent (manifest/stream requests),
#----- not the video-fetch UA which players spoof.
_APP_MAP = [
    ("nuvio", "Nuvio"),
    ("stremio", "Stremio"),
    ("vidi", "Vidi"),
    ("jellyfin", "Jellyfin"),
    ("emby", "Emby"),
    ("plex", "Plex"),
    ("infuse", "Infuse"),
    ("outplayer", "Outplayer"),
    ("iina", "IINA"),
    ("vlc", "VLC"),
    ("mpv", "mpv"),
    ("kodi", "Kodi"),
    ("xbmc", "Kodi"),
    ("mxplayer", "MX Player"),
    ("mxtech", "MX Player"),
    ("exoplayer", "ExoPlayer"),
    ("media3", "ExoPlayer"),
    ("applecoremedia", "Apple Player"),
    ("lavf", "FFmpeg / .strm"),
    ("ffmpeg", "FFmpeg / .strm"),
    ("libav", "FFmpeg / .strm"),
    ("okhttp", "Android App"),
    ("dalvik", "Android App"),
    ("ktor", "App"),
    ("cfnetwork", "iOS App"),
    ("edg", "Edge"),
    ("opr", "Opera"),
    ("firefox", "Firefox"),
    ("chrome", "Chrome"),
    ("safari", "Safari"),
    ("mozilla", "Browser"),
]

_DEVICE_MAP = [
    ("android tv", "Android TV"),
    ("androidtv", "Android TV"),
    ("googletv", "Android TV"),
    ("google tv", "Android TV"),
    ("bravia", "Android TV"),
    ("shield", "Android TV"),
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
    ("crkey", "Chromecast"),
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


#----- Real client IP behind Cloudflare / Caddy / reverse proxies
def client_ip_from(request) -> str:
    for h in ("cf-connecting-ip", "x-real-ip"):
        v = request.headers.get(h)
        if v:
            return v.strip()
    xff = request.headers.get("x-forwarded-for")
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
        return {"country": "Local", "country_code": "", "city": "", "isp": "", "proxy": False}
    now = time.time()
    cached = _IP_CACHE.get(ip)
    if cached and now - cached[1] < _IP_TTL:
        return cached[0]
    data = {"country": "", "country_code": "", "city": "", "isp": "", "proxy": False}
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,countryCode,city,isp,proxy,hosting"},
            )
            if resp.status_code == 200:
                j = resp.json()
                if j.get("status") == "success":
                    data = {
                        "country": j.get("country") or "",
                        "country_code": j.get("countryCode") or "",
                        "city": j.get("city") or "",
                        "isp": j.get("isp") or "",
                        "proxy": bool(j.get("proxy") or j.get("hosting")),
                    }
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] IP lookup failed for {ip}: {e}")
    _IP_CACHE[ip] = (data, now)
    return data


async def _record(token: str, name: str, ip: str, user_agent: str, is_client: bool) -> None:
    if not token:
        return
    coll = db.dbs["tracking"]["user_activity"]
    setf = {"last_active": datetime.utcnow(), "ip": ip or ""}
    #----- Only the addon-protocol request carries a trustworthy app/device UA.
    if is_client:
        setf["app"] = parse_app(user_agent)
        setf["device"] = parse_device(user_agent)
        setf["user_agent"] = user_agent or ""
    try:
        await coll.update_one(
            {"_id": token},
            {"$set": setf, "$setOnInsert": {"name": name or "Unknown"}},
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
    try:
        await coll.update_one({"_id": token}, {"$set": {
            "country": geo.get("country"),
            "country_code": geo.get("country_code"),
            "city": geo.get("city"),
            "isp": geo.get("isp"),
            "proxy": geo.get("proxy", False),
        }})
    except Exception as e:
        LOGGER.warning(f"[ANALYTICS] geo update failed: {e}")


#----- Called from the video byte-stream (/dl/): only refreshes presence, not device.
async def record_stream_start(token: str, name: str, ip: str, user_agent: str = "") -> None:
    await _record(token, name, ip, user_agent, is_client=False)


#----- Called from the addon protocol (stream/manifest): captures the real app/device.
async def record_client(token: str, name: str, ip: str, user_agent: str = "") -> None:
    await _record(token, name, ip, user_agent, is_client=True)


async def get_activity_overview(page: int = 1, per_page: int = 5) -> dict:
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=ONLINE_WINDOW)
    coll = db.dbs["tracking"]["user_activity"]
    tokens_coll = db.dbs["tracking"]["api_tokens"]

    valid_tokens = set()
    try:
        async for doc in tokens_coll.find({}, {"token": 1}):
            tok = doc.get("token")
            if tok:
                valid_tokens.add(tok)
    except Exception:
        valid_tokens = set()

    if valid_tokens:
        try:
            await coll.delete_many({"_id": {"$nin": list(valid_tokens)}})
        except Exception:
            pass

    playing = {}
    for info in ACTIVE_STREAMS.values():
        meta = info.get("meta", {}) or {}
        tok = meta.get("token")
        if tok and tok in valid_tokens:
            playing[tok] = meta.get("title") or "Streaming"

    match = {"_id": {"$in": list(valid_tokens)}} if valid_tokens else {"_id": {"$exists": False}}
    try:
        total = await coll.count_documents(match)
        online_count = await coll.count_documents({**match, "last_active": {"$gte": cutoff}})
    except Exception:
        total, online_count = 0, 0
    online_count = max(online_count, len(playing))

    per_page = max(1, min(int(per_page or 5), 60))
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page or 1), total_pages))
    offset = (page - 1) * per_page

    try:
        docs = await coll.find(match).sort("last_active", -1).skip(offset).limit(per_page).to_list(per_page)
    except Exception:
        docs = []

    rows = []
    for d in docs:
        token = d.get("_id")
        if token not in valid_tokens:
            continue
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
            "country_code": (d.get("country_code") or "").upper(),
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
