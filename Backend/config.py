from os import getenv, path
from dotenv import load_dotenv

load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"))

class Telegram:
    API_ID = int(getenv("API_ID", "0"))
    API_HASH = getenv("API_HASH", "")
    BOT_TOKEN = getenv("BOT_TOKEN", "")
    HELPER_BOT_TOKEN = getenv("HELPER_BOT_TOKEN", "")

    BASE_URL = getenv("BASE_URL", "").rstrip('/')
    PORT = int(getenv("PORT", "8000"))

    PARALLEL = int(getenv("PARALLEL", "1"))
    PRE_FETCH = int(getenv("PRE_FETCH", "1"))

    SMART_ROUTING_ENABLED = getenv("SMART_ROUTING_ENABLED", "true").lower() == "true"
    SMART_ROUTING_PROBE_ENABLED = getenv("SMART_ROUTING_PROBE_ENABLED", "true").lower() == "true"
    try:
        SMART_ROUTING_PROBE_CLIENTS = int(getenv("SMART_ROUTING_PROBE_CLIENTS", "3") or 3)
    except Exception:
        SMART_ROUTING_PROBE_CLIENTS = 3
    try:
        SMART_ROUTING_PROBE_BYTES = int(getenv("SMART_ROUTING_PROBE_BYTES", "262144") or 262144)
    except Exception:
        SMART_ROUTING_PROBE_BYTES = 262144
    try:
        SMART_ROUTING_PROBE_TIMEOUT_SEC = float(getenv("SMART_ROUTING_PROBE_TIMEOUT_SEC", "4") or 4)
    except Exception:
        SMART_ROUTING_PROBE_TIMEOUT_SEC = 4.0
    try:
        SMART_ROUTING_FIRST_CHUNK_TIMEOUT_SEC = float(getenv("SMART_ROUTING_FIRST_CHUNK_TIMEOUT_SEC", "4") or 4)
    except Exception:
        SMART_ROUTING_FIRST_CHUNK_TIMEOUT_SEC = 4.0
    try:
        SMART_ROUTING_CHUNK_TIMEOUT_SEC = float(getenv("SMART_ROUTING_CHUNK_TIMEOUT_SEC", "15") or 15)
    except Exception:
        SMART_ROUTING_CHUNK_TIMEOUT_SEC = 15.0

    AUTH_CHANNEL = [channel.strip() for channel in (getenv("AUTH_CHANNEL") or "").split(",") if channel.strip()]
    DATABASE = [db.strip() for db in (getenv("DATABASE") or "").split(",") if db.strip()]

    TMDB_API = getenv("TMDB_API", "")

    UPSTREAM_REPO = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "")

    OWNER_ID = int(getenv("OWNER_ID", "5422223708"))
    
    REPLACE_MODE = getenv("REPLACE_MODE", "true").lower() == "true"
    HIDE_CATALOG = getenv("HIDE_CATALOG", "false").lower() == "true"

    ADMIN_USERNAME = getenv("ADMIN_USERNAME", "fyvio")
    ADMIN_PASSWORD = getenv("ADMIN_PASSWORD", "fyvio")
    DEFAULT_ADDON_TOKEN = getenv("DEFAULT_ADDON_TOKEN", "").strip()
    
    SUBSCRIPTION = getenv("SUBSCRIPTION", "false").lower() == "true"
    SUBSCRIPTION_GROUP_ID = int(getenv("SUBSCRIPTION_GROUP_ID", "0"))
    SUBSCRIPTION_URL = getenv("SUBSCRIPTION_URL", "https://t.me/")
    APPROVER_IDS = [int(x.strip()) for x in (getenv("APPROVER_IDS") or "").split(",") if x.strip().isdigit()]

    PROXY = getenv("Proxy", "false").lower() == "true"
    PROXY_TYPE = getenv("ProxyType", "HTTPS")
    HTTP_PROXY_URL = getenv("HTTP_Proxy_URL", "")
    SHOW_PROXY_AND_NON_PROXY_BOTH = getenv("SHOW_ProxyAndNonProxyBoth", "false").lower() == "true"

    # -------------------------------
    # Disk cache + nginx offload (optional)
    # -------------------------------
    DISK_CACHE_ENABLED = getenv("DISK_CACHE_ENABLED", "false").lower() == "true"
    DISK_CACHE_DIR = getenv("DISK_CACHE_DIR", "cache")
    try:
        DISK_CACHE_MAX_GB = float(getenv("DISK_CACHE_MAX_GB", "0") or 0)
    except Exception:
        DISK_CACHE_MAX_GB = 0.0
    try:
        DISK_CACHE_MAX_BYTES = int(getenv("DISK_CACHE_MAX_BYTES", "0") or 0)
    except Exception:
        DISK_CACHE_MAX_BYTES = 0

    DISK_CACHE_CONCURRENCY = int(getenv("DISK_CACHE_CONCURRENCY", "1") or 1)
    DISK_CACHE_PRECACHE_ON_INGEST = getenv("DISK_CACHE_PRECACHE_ON_INGEST", "false").lower() == "true"

    NGINX_ACCEL_REDIRECT_ENABLED = getenv("NGINX_ACCEL_REDIRECT_ENABLED", "false").lower() == "true"
    NGINX_ACCEL_REDIRECT_LOCATION = getenv("NGINX_ACCEL_REDIRECT_LOCATION", "/_cache/")

    # -------------------------------
    # Streaming SLO warnings (logs only)
    # -------------------------------
    try:
        STREAM_SLO_TTFB_WARN_SEC = float(getenv("STREAM_SLO_TTFB_WARN_SEC", "3") or 3)
    except Exception:
        STREAM_SLO_TTFB_WARN_SEC = 3.0
    try:
        STREAM_SLO_TIMEOUT_WARN_COUNT = int(getenv("STREAM_SLO_TIMEOUT_WARN_COUNT", "2") or 2)
    except Exception:
        STREAM_SLO_TIMEOUT_WARN_COUNT = 2
    try:
        STREAM_SLO_BUFFERING_WARN_RATE = float(getenv("STREAM_SLO_BUFFERING_WARN_RATE", "0.05") or 0.05)
    except Exception:
        STREAM_SLO_BUFFERING_WARN_RATE = 0.05

    # -------------------------------
    # Torrent tracker scrape stats (optional, lightweight)
    # -------------------------------
    TORRENT_STATS_ENABLED = getenv("TORRENT_STATS_ENABLED", "true").lower() == "true"
    try:
        TORRENT_STATS_TTL_SEC = int(getenv("TORRENT_STATS_TTL_SEC", "21600") or 21600)
    except Exception:
        TORRENT_STATS_TTL_SEC = 21600
    try:
        TORRENT_STATS_FAILURE_TTL_SEC = int(getenv("TORRENT_STATS_FAILURE_TTL_SEC", "3600") or 3600)
    except Exception:
        TORRENT_STATS_FAILURE_TTL_SEC = 3600
    try:
        TORRENT_STATS_MAX_TRACKERS = int(getenv("TORRENT_STATS_MAX_TRACKERS", "5") or 5)
    except Exception:
        TORRENT_STATS_MAX_TRACKERS = 5
    try:
        TORRENT_STATS_TIMEOUT_SEC = float(getenv("TORRENT_STATS_TIMEOUT_SEC", "2.5") or 2.5)
    except Exception:
        TORRENT_STATS_TIMEOUT_SEC = 2.5
    try:
        TORRENT_STATS_CONCURRENCY = int(getenv("TORRENT_STATS_CONCURRENCY", "3") or 3)
    except Exception:
        TORRENT_STATS_CONCURRENCY = 3
