from datetime import datetime

from bson import ObjectId
from pymongo import ReturnDocument
from pyrogram.enums import ParseMode

from Backend import db
from Backend.config import Telegram
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER
from Backend.pyrofork.bot import StreamBot

STATUSES = ("pending", "fulfilled", "rejected")
MAX_TEXT = 300


def _coll():
    return db.dbs["tracking"]["requests"]


def _approver_ids():
    return SettingsManager.current().approver_ids or [Telegram.OWNER_ID]


async def create_request(user_id, user_name: str, username, text: str) -> dict:
    now = datetime.utcnow()
    doc = {
        "user_id": int(user_id),
        "user_name": user_name or f"User {user_id}",
        "username": username,
        "text": text.strip()[:MAX_TEXT],
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    result = await _coll().insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc


async def list_requests(status=None, limit: int = 300) -> list:
    query = {"status": status} if status in STATUSES else {}
    items = []
    async for doc in _coll().find(query).sort("created_at", -1).limit(limit):
        doc["_id"] = str(doc["_id"])
        items.append(doc)
    return items


async def request_counts() -> dict:
    counts = {"pending": 0, "fulfilled": 0, "rejected": 0, "total": 0}
    try:
        for status in STATUSES:
            counts[status] = await _coll().count_documents({"status": status})
        counts["total"] = sum(counts[s] for s in STATUSES)
    except Exception as e:
        LOGGER.warning(f"[REQUEST] count failed: {e}")
    return counts


async def set_status(request_id: str, status: str):
    if status not in STATUSES:
        return None
    try:
        oid = ObjectId(request_id)
    except Exception:
        return None
    doc = await _coll().find_one_and_update(
        {"_id": oid},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return doc


async def delete_request(request_id: str) -> bool:
    try:
        oid = ObjectId(request_id)
    except Exception:
        return False
    result = await _coll().delete_one({"_id": oid})
    return result.deleted_count > 0


async def notify_approvers(text: str) -> None:
    for approver_id in _approver_ids():
        try:
            await StreamBot.send_message(approver_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            LOGGER.warning(f"[REQUEST] notify approver {approver_id} failed: {e}")


async def notify_user(user_id, text: str) -> bool:
    try:
        await StreamBot.send_message(int(user_id), text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return True
    except Exception as e:
        LOGGER.warning(f"[REQUEST] notify user {user_id} failed: {e}")
        return False
