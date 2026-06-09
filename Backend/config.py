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

    AUTH_CHANNEL = [channel.strip() for channel in (getenv("AUTH_CHANNEL") or "").split(",") if channel.strip()]
    DATABASE = [db.strip() for db in (getenv("DATABASE") or "").split(",") if db.strip()]

    TMDB_API = getenv("TMDB_API", "")

    UPSTREAM_REPO = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "")

    OWNER_ID = int(getenv("OWNER_ID", "5422223708"))
    
    REPLACE_MODE = getenv("REPLACE_MODE", "true").lower() == "true"
    HIDE_CATALOG = getenv("HIDE_CATALOG", "false").lower() == "true"

    # IPTV live TV (iptv-org, India v1)
    IPTV_ENABLED = getenv("IPTV_ENABLED", "false").lower() == "true"
    IPTV_COUNTRY_CODES = [
        code.strip().upper()
        for code in getenv("IPTV_COUNTRY_CODES", "IN").split(",")
        if code.strip()
    ] or ["IN"]
    try:
        IPTV_PAGE_SIZE = max(1, min(100, int(getenv("IPTV_PAGE_SIZE", "50") or 50)))
    except Exception:
        IPTV_PAGE_SIZE = 50
    IPTV_AUTO_SYNC = getenv("IPTV_AUTO_SYNC", "true").lower() == "true"
    try:
        IPTV_SYNC_INTERVAL_MINUTES = max(30, int(getenv("IPTV_SYNC_INTERVAL_MINUTES", "360") or 360))
    except Exception:
        IPTV_SYNC_INTERVAL_MINUTES = 360
    try:
        IPTV_SYNC_START_DELAY_SECONDS = max(0, int(getenv("IPTV_SYNC_START_DELAY_SECONDS", "30") or 30))
    except Exception:
        IPTV_SYNC_START_DELAY_SECONDS = 30
    try:
        IPTV_REQUEST_TIMEOUT_SEC = max(5.0, float(getenv("IPTV_REQUEST_TIMEOUT_SEC", "45") or 45))
    except Exception:
        IPTV_REQUEST_TIMEOUT_SEC = 45.0
    try:
        IPTV_PROXY_TIMEOUT_SEC = max(5.0, float(getenv("IPTV_PROXY_TIMEOUT_SEC", "30") or 30))
    except Exception:
        IPTV_PROXY_TIMEOUT_SEC = 30.0
    IPTV_PROXY_FALLBACK_ENABLED = getenv("IPTV_PROXY_FALLBACK_ENABLED", "true").lower() == "true"
    IPTV_PROXY_SECRET = getenv("IPTV_PROXY_SECRET", "").strip()
    IPTV_API_BASE_URL = getenv("IPTV_API_BASE_URL", "https://iptv-org.github.io/api").rstrip("/")

    ADMIN_USERNAME = getenv("ADMIN_USERNAME", "fyvio")
    ADMIN_PASSWORD = getenv("ADMIN_PASSWORD", "fyvio")
    
    SUBSCRIPTION = getenv("SUBSCRIPTION", "false").lower() == "true"
    SUBSCRIPTION_GROUP_ID = int(getenv("SUBSCRIPTION_GROUP_ID", "0"))
    SUBSCRIPTION_URL = getenv("SUBSCRIPTION_URL", "https://t.me/")
    APPROVER_IDS = [int(x.strip()) for x in (getenv("APPROVER_IDS") or "").split(",") if x.strip().isdigit()]

    PROXY = getenv("Proxy", "false").lower() == "true"
    PROXY_TYPE = getenv("ProxyType", "HTTPS")
    HTTP_PROXY_URL = getenv("HTTP_Proxy_URL", "")
    SHOW_PROXY_AND_NON_PROXY_BOTH = getenv("SHOW_ProxyAndNonProxyBoth", "false").lower() == "true"
