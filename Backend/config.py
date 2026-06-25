from os import getenv, path
from dotenv import load_dotenv

load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"))


class Telegram:
    # ── Required: Telegram clients ───────────────────────────────────────────
    API_ID           = int(getenv("API_ID", "0"))
    API_HASH         = getenv("API_HASH", "")
    BOT_TOKEN        = getenv("BOT_TOKEN", "")
    USER_SESSION_STRING = getenv("USER_SESSION_STRING", "")

    # ── Required: Database URIs ───────────────────────────────────────────────
    DATABASE = [db.strip() for db in (getenv("DATABASE") or "").split(",") if db.strip()]

    # ── Required: Server ─────────────────────────────────────────────────────
    PORT     = int(getenv("PORT", "8000"))
    OWNER_ID = int(getenv("OWNER_ID", "0"))

    # ── Legacy migration fallbacks ────────────────────────────────────────────
    # These are READ ONCE by SettingsManager._seed_from_env() on first startup
    # and then stored in the database.  Do NOT reference them elsewhere in the
    # codebase — use SettingsManager.current().<property> instead.
    REPLACE_MODE                 = getenv("REPLACE_MODE", "true").lower() == "true"
    HIDE_CATALOG                 = getenv("HIDE_CATALOG", "false").lower() == "true"
    AUTH_CHANNEL                 = [c.strip() for c in (getenv("AUTH_CHANNEL") or "").split(",") if c.strip()]
    TMDB_API                     = getenv("TMDB_API", "")
    BASE_URL                     = getenv("BASE_URL", "").rstrip("/")
    UPSTREAM_REPO                = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH              = getenv("UPSTREAM_BRANCH", "")
    ADMIN_USERNAME               = getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD               = getenv("ADMIN_PASSWORD", "admin")
    SUBSCRIPTION                 = getenv("SUBSCRIPTION", "false").lower() == "true"
    SUBSCRIPTION_GROUP_ID        = int(getenv("SUBSCRIPTION_GROUP_ID", "0"))
    APPROVER_IDS                 = [int(x.strip()) for x in (getenv("APPROVER_IDS") or "").split(",") if x.strip().isdigit()]
    HTTP_PROXY_URL               = getenv("HTTP_Proxy_URL", "")
    SHOW_PROXY_AND_NON_PROXY_BOTH = getenv("SHOW_ProxyAndNonProxyBoth", "false").lower() == "true"
