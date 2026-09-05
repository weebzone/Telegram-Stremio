from __future__ import annotations

from pyrogram import Client

from Backend.config import Telegram
from Backend.helper.settings.manager import SettingsManager


def currency_symbol(code: str | None) -> str:
    return {
        "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
        "AUD": "A$", "CAD": "C$", "SGD": "S$", "AED": "د.إ", "BRL": "R$",
    }.get((code or "INR").upper(), f"{(code or 'INR')} ")


def approver_ids() -> list:
    return SettingsManager.current().approver_ids or [Telegram.OWNER_ID]


async def resolve_target_info(client: Client, target_user_id: int):
    try:
        target_user = await client.get_users(target_user_id)
        mention = target_user.mention
        username = f"@{target_user.username}" if target_user.username else "N/A"
        return mention, username
    except Exception:
        return f"<a href='tg://user?id={target_user_id}'>User</a>", "N/A"
