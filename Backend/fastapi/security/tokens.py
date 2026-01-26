from fastapi import HTTPException
from Backend import db

DAILY_LIMIT_VIDEO = "https://bit.ly/3YZFKT5"
MONTHLY_LIMIT_VIDEO = "https://bit.ly/4rfjtgd"


async def verify_token(token: str):
    token_data = await db.get_api_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired API token")

    limits = token_data.get("limits", {})
    usage = token_data.get("usage", {})

    daily_limit = limits.get("daily_limit_gb")
    monthly_limit = limits.get("monthly_limit_gb")

    token_data["limit_exceeded"] = None
    token_data["limit_video"] = None

    if daily_limit and daily_limit > 0:
        current_daily_gb = usage.get("daily", {}).get("bytes", 0) / (1024 ** 3)
        if current_daily_gb >= daily_limit:
            token_data["limit_exceeded"] = "daily"
            token_data["limit_video"] = DAILY_LIMIT_VIDEO
            return token_data

    if monthly_limit and monthly_limit > 0:
        current_monthly_gb = usage.get("monthly", {}).get("bytes", 0) / (1024 ** 3)
        if current_monthly_gb >= monthly_limit:
            token_data["limit_exceeded"] = "monthly"
            token_data["limit_video"] = MONTHLY_LIMIT_VIDEO
            return token_data

    return token_data
