from datetime import datetime, timezone

from fastapi import HTTPException

from Backend import db
from Backend.helper.settings_manager import SettingsManager

DAILY_LIMIT_VIDEO = "https://bit.ly/3YZFKT5"
MONTHLY_LIMIT_VIDEO = "https://bit.ly/4rfjtgd"
SUBSCRIPTION_EXPIRED_VIDEO = "https://bit.ly/4rfjtgd"


#----- Validate an API token and annotate it with subscription/limit status
async def verify_token(token: str):
    token_data = await db.get_api_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired API token")

    limits = token_data.get("limits", {})
    usage = token_data.get("usage", {})

    token_data["limit_exceeded"] = None
    token_data["limit_video"] = None
    token_data["subscription_expired"] = False

    #----- Subscription expiry check (only when the SUBSCRIPTION feature is enabled)
    if SettingsManager.current().subscription:
        user_id = token_data.get("user_id")
        if not user_id:
            token_data["subscription_expired"] = True
            return token_data

        user = await db.get_user(int(user_id))
        if not user or user.get("subscription_status") != "active":
            token_data["subscription_expired"] = True
            return token_data

        expiry = user.get("subscription_expiry")
        if not expiry:
            token_data["subscription_expired"] = True
            return token_data

        #----- Compare correctly regardless of timezone awareness
        now = datetime.utcnow()
        try:
            if expiry.tzinfo is not None:
                now = datetime.now(timezone.utc)
        except AttributeError:
            pass
        if expiry < now:
            token_data["subscription_expired"] = True
            return token_data

    if daily_limit := limits.get("daily_limit_gb"):
        if daily_limit > 0:
            current_daily_gb = usage.get("daily", {}).get("bytes", 0) / (1024 ** 3)
            if current_daily_gb >= daily_limit:
                token_data["limit_exceeded"] = "daily"
                token_data["limit_video"] = DAILY_LIMIT_VIDEO
                return token_data

    if monthly_limit := limits.get("monthly_limit_gb"):
        if monthly_limit > 0:
            current_monthly_gb = usage.get("monthly", {}).get("bytes", 0) / (1024 ** 3)
            if current_monthly_gb >= monthly_limit:
                token_data["limit_exceeded"] = "monthly"
                token_data["limit_video"] = MONTHLY_LIMIT_VIDEO
                return token_data

    return token_data
