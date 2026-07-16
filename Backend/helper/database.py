import re
import secrets
import string
from asyncio import create_task
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import motor.motor_asyncio
from bson import ObjectId
from pydantic import ValidationError
from pymongo import ASCENDING, DESCENDING

from Backend.config import Telegram
from Backend.helper.encrypt import decode_string, encode_string
from Backend.helper.modal import Episode, MovieSchema, QualityDetail, QualityPart, Season, TVShowSchema
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.task_manager import delete_message
from Backend.logger import LOGGER



def convert_objectid_to_str(document: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in document.items():
        if isinstance(value, ObjectId):
            document[key] = str(value)
        elif isinstance(value, list):
            document[key] = [convert_objectid_to_str(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            document[key] = convert_objectid_to_str(value)
    return document


class Database:
    def __init__(self, db_name: str = "dbFyvio"):
        self.db_uris = Telegram.DATABASE
        self.db_name = db_name

        if len(self.db_uris) < 2:
            raise ValueError("At least 2 database URIs are required (1 for tracking + 1 for storage).")

        self.clients: Dict[str, motor.motor_asyncio.AsyncIOMotorClient] = {}
        self.dbs: Dict[str, motor.motor_asyncio.AsyncIOMotorDatabase] = {}

        self.current_db_index = 1

    async def connect(self):
        try:
            for index, uri in enumerate(self.db_uris):
                client = motor.motor_asyncio.AsyncIOMotorClient(uri)
                db_key = "tracking" if index == 0 else f"storage_{index}"
                self.clients[db_key] = client
                self.dbs[db_key] = client[self.db_name]
                db_type = "Tracking" if index == 0 else f"Storage {index}"

                masked_uri = re.sub(r"://(.*?):.*?@", r"://\1:*****@", uri)
                masked_uri = masked_uri.split('?')[0]
                
                LOGGER.info(f"{db_type} Database connected successfully: {masked_uri}")

            state = await self.dbs["tracking"]["state"].find_one({"_id": "db_index"})
            if not state:
                await self.dbs["tracking"]["state"].insert_one({"_id": "db_index", "current_index": 1})
                self.current_db_index = 1
            else:
                self.current_db_index = state["current_index"]

            LOGGER.info(f"Active storage DB: storage_{self.current_db_index}")

            await self.ensure_indexes()

        except Exception as e:
            LOGGER.error(f"Database connection error: {e}")

    #----- Create the indexes catalog/stream lookups rely on.
    #----- create_index is idempotent, so this is safe to call repeatedly.
    async def ensure_indexes(self) -> None:
        tracking = self.dbs.get("tracking")
        if tracking is not None:
            try:
                await tracking["custom_catalogs"].create_index([("updated_at", DESCENDING)])
                await tracking["custom_catalogs"].create_index(
                    [("items.tmdb_id", ASCENDING), ("items.media_type", ASCENDING)]
                )
                await self._ensure_subtitle_indexes(tracking)
            except Exception as e:
                LOGGER.error(f"Failed creating tracking indexes: {e}")

        for db_key in list(self.dbs.keys()):
            if db_key.startswith("storage_"):
                await self._ensure_storage_indexes(db_key)

    async def _ensure_subtitle_indexes(self, tracking) -> None:
        subs = tracking["subtitles"]
        try:
            info = await subs.index_information()
        except Exception:
            info = {}
        for name, spec in info.items():
            if name == "_id_":
                continue
            keys = [k for k, _ in spec.get("key", [])]
            if keys == ["stream_id"] or (spec.get("unique") and "stream_id" in keys):
                try:
                    await subs.drop_index(name)
                    LOGGER.info(f"Dropped stale subtitle index {name}")
                except Exception as e:
                    LOGGER.error(f"Failed dropping subtitle index {name}: {e}")
        await subs.create_index([("chat_id", ASCENDING), ("msg_id", ASCENDING)], unique=True)
        await subs.create_index([("imdb_id", ASCENDING), ("season", ASCENDING), ("episode", ASCENDING)])

    #----- Ensure per-storage-DB indexes on the movie/tv collections.
    #----- tmdb_id + imdb_id drive catalog hydration and stream lookups.
    async def _ensure_storage_indexes(self, db_key: str) -> None:
        db = self.dbs.get(db_key)
        if db is None:
            return
        for collection_name in ("movie", "tv"):
            try:
                await db[collection_name].create_index([("tmdb_id", ASCENDING)])
                await db[collection_name].create_index([("imdb_id", ASCENDING)])
            except Exception as e:
                LOGGER.error(f"Failed creating index on {db_key}/{collection_name}: {e}")

    async def disconnect(self):
        for client in self.clients.values():
            client.close()
        LOGGER.info("All database connections closed.")

    async def update_current_db_index(self):
        await self.dbs["tracking"]["state"].update_one(
            {"_id": "db_index"},
            {"$set": {"current_index": self.current_db_index}},
            upsert=True
        )


    async def get_settings(self) -> dict:
        try:
            doc = await self.dbs["tracking"]["settings"].find_one({"_id": "app_settings"})
            return doc or {}
        except Exception as e:
            LOGGER.error(f"Database.get_settings error: {e}")
            return {}

    async def save_settings(self, settings: dict) -> bool:
        try:
            clean = {k: v for k, v in settings.items() if k != "_id"}
            await self.dbs["tracking"]["settings"].update_one(
                {"_id": "app_settings"},
                {"$set": clean},
                upsert=True,
            )
            return True
        except Exception as e:
            LOGGER.error(f"Database.save_settings error: {e}")
            return False



    async def connect_storage_db(self, uri: str, index: int) -> bool:
        try:
            client = motor.motor_asyncio.AsyncIOMotorClient(uri)
            await client.admin.command("ping")

            db_key = "tracking" if index == 0 else f"storage_{index}"
            self.clients[db_key] = client
            self.dbs[db_key] = client[self.db_name]

            db_type = "Tracking" if index == 0 else f"Storage {index}"
            masked_uri = re.sub(r"://(.*?):.*?@", r"://\1:*****@", uri).split('?')[0]
            LOGGER.info(f"{db_type} Database connected successfully: {masked_uri}")
            if index > 0:
                await self._ensure_storage_indexes(db_key)
            return True
        except Exception as e:
            LOGGER.error(f"Failed to connect database at index {index}: {e}")
            return False

    async def disconnect_storage_db(self, index: int) -> None:
        db_key = f"storage_{index}"
        client = self.clients.pop(db_key, None)
        self.dbs.pop(db_key, None)
        if client:
            client.close()
            LOGGER.info(f"Disconnected {db_key}.")

    def get_database_list(self) -> List[Dict[str, Any]]:
        result = []
        for index, uri in enumerate(self.db_uris):
            masked = re.sub(r"://(.*?):.*?@", r"://\1:*****@", uri).split('?')[0]
            db_key = "tracking" if index == 0 else f"storage_{index}"
            entry = {
                "index": index,
                "uri_masked": masked,
                "locked": index <= 1,
                "type": "tracking" if index == 0 else "storage",
                "connected": db_key in self.clients,
            }
            if index > 1:
                entry["full_uri"] = uri
            result.append(entry)
        return result

    async def reload_extra_databases(self, new_extra_uris: List[str]) -> Dict[str, Any]:
        old_extra = self.db_uris[2:]
        new_extra = [u.strip() for u in (new_extra_uris or []) if u and u.strip()]

        common_len = min(len(old_extra), len(new_extra))
        for i in range(common_len):
            if old_extra[i] != new_extra[i]:
                raise ValueError(
                    f"Cannot modify storage_{i + 2} in place — existing media "
                    f"documents reference it by position. Only appending new "
                    f"databases or removing the last one(s) is supported."
                )

        added = 0
        removed = 0

        if len(new_extra) > len(old_extra):
            for offset, uri in enumerate(new_extra[len(old_extra):]):
                index = len(old_extra) + 2 + offset
                ok = await self.connect_storage_db(uri, index)
                if not ok:
                    raise ValueError(
                        f"Failed to connect new database at storage_{index}. "
                        f"Check the URI and try again — no changes were saved."
                    )
                added += 1

        elif len(new_extra) < len(old_extra):
            for index in range(len(old_extra) + 1, len(new_extra) + 1, -1):
                await self.disconnect_storage_db(index)
                removed += 1

        self.db_uris = self.db_uris[:2] + new_extra

        message = f"{added} database(s) added, {removed} database(s) removed."
        LOGGER.info(f"reload_extra_databases: {message}")
        return {"added": added, "removed": removed, "message": message}

    #-----
    #----- User Subscription Management
    #-----
    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.dbs["tracking"]["users"].find_one({"_id": user_id})

    #----- Whether a user doc represents a currently-active subscription
    @staticmethod
    def is_subscription_active(user: Optional[dict], now: datetime = None) -> bool:
        if not user or user.get("subscription_status") != "active":
            return False
        expiry = user.get("subscription_expiry")
        if not expiry:
            return False
        reference = now or datetime.utcnow()
        try:
            if expiry.tzinfo is not None:
                reference = datetime.now(timezone.utc)
        except AttributeError:
            pass
        return expiry > reference

    #----- (movie_count, tv_count) totals summed across per-DB stats
    @staticmethod
    def content_totals(db_stats: List[dict]) -> Tuple[int, int]:
        total_movies = sum(stat.get("movie_count", 0) for stat in db_stats)
        total_tv = sum(stat.get("tv_count", 0) for stat in db_stats)
        return total_movies, total_tv

    async def update_user_interaction(self, user_id: int, first_name: str, username: str):
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"first_name": first_name, "username": username, "last_interaction": datetime.utcnow()}},
            upsert=True
        )

    async def set_pending_payment(self, user_id: int, plan_duration: int, msg_id: int, price=0, admin_messages: list = None):
        update_data = {
            "pending_payment": {
                "duration": plan_duration,
                "price": price,
                "msg_id": msg_id,
                "date": datetime.utcnow(),
            }
        }
        if admin_messages is not None:
            update_data["pending_payment"]["admin_messages"] = admin_messages
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": update_data},
            upsert=True
        )

    async def approve_payment(self, user_id: int) -> Optional[dict]:
        user = await self.get_user(user_id)
        if not user or "pending_payment" not in user:
            return None

        duration = user["pending_payment"]["duration"]
        
        #----- Calculate new expiry
        current_expiry = user.get("subscription_expiry")
        now = datetime.utcnow()
        if current_expiry and current_expiry > now:
            new_expiry = current_expiry + timedelta(days=duration)
        else:
            new_expiry = now + timedelta(days=duration)

        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {
                "$set": {"subscription_expiry": new_expiry, "subscription_status": "active"},
                "$unset": {"pending_payment": ""}
            }
        )
        return await self.get_user(user_id)

    async def reject_payment(self, user_id: int) -> bool:
        result = await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$unset": {"pending_payment": ""}}
        )
        return result.modified_count > 0

    async def get_expired_users(self) -> List[dict]:
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_expiry": {"$lt": datetime.utcnow()},
            "subscription_status": "active"
        })
        return await cursor.to_list(None)

    async def mark_user_expired(self, user_id: int):
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"subscription_status": "expired"}}
        )

    async def get_expiring_users(self, hours: int = 24) -> List[dict]:
        now = datetime.utcnow()
        target_time = now + timedelta(hours=hours)
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_expiry": {"$gt": now, "$lte": target_time},
            "reminder_sent": {"$ne": True},
            "subscription_status": "active"
        })
        return await cursor.to_list(None)
        
    async def mark_reminder_sent(self, user_id: int):
         await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": {"reminder_sent": True}}
        )

    #-----
    #----- Admin Subscription Management
    #-----
    async def get_subscription_plans(self) -> List[dict]:
        cursor = self.dbs["tracking"]["sub_plans"].find().sort("days", ASCENDING)
        plans = await cursor.to_list(None)
        return [convert_objectid_to_str(plan) for plan in plans]

    async def add_subscription_plan(self, days: int, price: float) -> Optional[str]:
        result = await self.dbs["tracking"]["sub_plans"].insert_one({
            "days": days,
            "price": price,
            "created_at": datetime.utcnow()
        })
        return str(result.inserted_id)

    async def update_subscription_plan(self, plan_id: str, days: int, price: float) -> bool:
        try:
            result = await self.dbs["tracking"]["sub_plans"].update_one(
                {"_id": ObjectId(plan_id)},
                {"$set": {"days": days, "price": price, "updated_at": datetime.utcnow()}}
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def delete_subscription_plan(self, plan_id: str) -> bool:
        try:
            result = await self.dbs["tracking"]["sub_plans"].delete_one({"_id": ObjectId(plan_id)})
            return result.deleted_count > 0
        except Exception:
            return False

    async def get_all_subscribers(self) -> List[dict]:
        cursor = self.dbs["tracking"]["users"].find({
            "subscription_status": {"$in": ["active", "expired"]}
        }).sort("subscription_expiry", DESCENDING)
        users = await cursor.to_list(None)
        return [convert_objectid_to_str(u) for u in users]

    async def manage_subscriber(self, user_id: int, action: str, days: int = 0) -> bool:
        now = datetime.utcnow()
        user = await self.get_user(user_id)

        if action in ("extend", "reduce"):
            current_expiry = user.get("subscription_expiry") if user else None

            if action == "extend":
                base = current_expiry if (current_expiry and current_expiry > now) else now
                new_expiry = base + timedelta(days=days)
            else:
                if current_expiry:
                    new_expiry = current_expiry - timedelta(days=days)
                    if new_expiry < now:
                        new_expiry = now
                else:
                    new_expiry = now

            status = "active" if new_expiry > now else "expired"

            await self.dbs["tracking"]["users"].update_one(
                {"_id": user_id},
                {
                    "$set": {"subscription_expiry": new_expiry, "subscription_status": status},
                    "$setOnInsert": {
                        "first_name": f"User {user_id}",
                        "username": None,
                        "created_at": now,
                    },
                },
                upsert=True
            )
            if status == "active":
                await self.ensure_api_token_for_user(user_id, (user or {}).get("first_name"))
            await self.align_token_with_subscription(user_id)
            return True

        elif action == "delete":
            await self.dbs["tracking"]["users"].update_one(
                {"_id": user_id},
                {"$set": {"subscription_status": "expired", "subscription_expiry": now}}
            )
            await self.align_token_with_subscription(user_id)
            return True

        elif action == "remove":
            await self.align_token_with_subscription(user_id)
            await self.dbs["tracking"]["users"].delete_one({"_id": user_id})
            return True

        return False

    #----- Update a subscriber's display name (and the linked token's name)
    async def update_subscriber_name(self, user_id: int, name: str) -> None:
        await self.dbs["tracking"]["users"].update_one({"_id": user_id}, {"$set": {"first_name": name}})
        await self.dbs["tracking"]["api_tokens"].update_one({"user_id": user_id}, {"$set": {"name": name}})

    async def assign_subscription(self, user_id: int, days: int, name: str = None) -> dict:
        #----- Upsert a subscription for any user_id, creating a record if it doesn't exist
        now = datetime.utcnow()

        user = await self.get_user(user_id)
        if user:
            current_expiry = user.get("subscription_expiry")
            if current_expiry and current_expiry > now:
                new_expiry = current_expiry + timedelta(days=days)
            else:
                new_expiry = now + timedelta(days=days)
        else:
            new_expiry = now + timedelta(days=days)

        set_fields = {"subscription_expiry": new_expiry, "subscription_status": "active"}
        insert_fields = {"_id": user_id, "username": None, "created_at": now}
        if name:
            set_fields["first_name"] = name
        else:
            insert_fields["first_name"] = f"User {user_id}"

        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": set_fields, "$setOnInsert": insert_fields},
            upsert=True
        )
        token_doc = await self.ensure_api_token_for_user(user_id, (user or {}).get("first_name"))
        token = token_doc.get("token") if token_doc else None
        await self.align_token_with_subscription(user_id)
        return {
            "user_id": user_id,
            "subscription_expiry": new_expiry.isoformat(),
            "subscription_status": "active",
            "days_assigned": days,
            "token": token,
            "addon_url": (
                f"{SettingsManager.current().base_url}/stremio/{token}/manifest.json" if token else None
            ),
        }

    #----- Give a user's token never-expiring access (clears any expiry date)
    async def set_user_never_expires(self, user_id: int, name: str = None) -> dict:
        now = datetime.utcnow()
        user = await self.get_user(user_id)
        set_fields = {"subscription_status": "active"}
        insert_fields = {"username": None, "created_at": now}
        if name:
            set_fields["first_name"] = name
        else:
            insert_fields["first_name"] = f"User {user_id}"
        await self.dbs["tracking"]["users"].update_one(
            {"_id": user_id},
            {"$set": set_fields, "$unset": {"subscription_expiry": ""}, "$setOnInsert": insert_fields},
            upsert=True,
        )
        token_doc = await self.ensure_api_token_for_user(user_id, (user or {}).get("first_name"))
        token = token_doc.get("token") if token_doc else None
        if token:
            await self.set_token_lifetime(token, True)
        return {
            "user_id": user_id,
            "never_expires": True,
            "token": token,
            "addon_url": (
                f"{SettingsManager.current().base_url}/stremio/{token}/manifest.json" if token else None
            ),
        }

    #-----
    #----- Custom Catalog Management
    #-----
    #----- Backfill the visibility model on catalogs that predate it
    @staticmethod
    def _normalize_catalog(catalog: Optional[dict]) -> Optional[dict]:
        if not catalog:
            return catalog
        if catalog.get("visibility") not in ("public", "tokens", "owner"):
            catalog["visibility"] = "public" if catalog.get("visible", True) else "owner"
        catalog.setdefault("allowed_tokens", [])
        catalog.setdefault("exclusive", False)
        catalog.setdefault("searchable", False)
        return catalog

    async def create_custom_catalog(self, name: str, visibility: str = "public", allowed_tokens: Optional[List[str]] = None) -> Optional[str]:
        name = (name or "").strip()
        if not name:
            return None

        if visibility not in ("public", "tokens", "owner"):
            visibility = "public"
        now = datetime.utcnow()
        result = await self.dbs["tracking"]["custom_catalogs"].insert_one({
            "name": name,
            "visibility": visibility,
            "allowed_tokens": list(allowed_tokens or []),
            "visible": visibility != "owner",
            "exclusive": False,
            "searchable": False,
            "items": [],
            "created_at": now,
            "updated_at": now,
        })
        return str(result.inserted_id)

    async def get_custom_catalogs(self, visible_only: bool = False) -> List[dict]:
        query = {"visible": True} if visible_only else {}
        cursor = self.dbs["tracking"]["custom_catalogs"].find(query).sort("updated_at", DESCENDING)
        catalogs = await cursor.to_list(None)
        return [self._normalize_catalog(convert_objectid_to_str(catalog)) for catalog in catalogs]

    async def get_custom_catalog(self, catalog_id: str) -> Optional[dict]:
        try:
            catalog = await self.dbs["tracking"]["custom_catalogs"].find_one({"_id": ObjectId(catalog_id)})
            return self._normalize_catalog(convert_objectid_to_str(catalog)) if catalog else None
        except Exception:
            return None

    async def update_custom_catalog(
        self,
        catalog_id: str,
        name: Optional[str] = None,
        visibility: Optional[str] = None,
        allowed_tokens: Optional[List[str]] = None,
        exclusive: Optional[bool] = None,
        searchable: Optional[bool] = None,
    ) -> bool:
        try:
            existing = await self.dbs["tracking"]["custom_catalogs"].find_one({"_id": ObjectId(catalog_id)})
        except Exception:
            return False
        if not existing:
            return False

        update_data = {"updated_at": datetime.utcnow()}
        if name is not None:
            clean_name = name.strip()
            if clean_name:
                update_data["name"] = clean_name

        #----- Setting catalog visibility cascades to every title in the catalog
        cascade = visibility in ("public", "tokens", "owner")
        tokens = list(allowed_tokens or [])
        final_visibility = visibility if cascade else existing.get("visibility", "public")
        if cascade:
            update_data["visibility"] = visibility
            update_data["visible"] = visibility != "owner"
            update_data["allowed_tokens"] = tokens
            update_data["items.$[].visibility"] = visibility
            update_data["items.$[].allowed_tokens"] = tokens

        #----- Exclusive locks every title to this catalog only (never on auto catalogs,
        #----- and only meaningful for restricted visibility)
        is_auto = bool(existing.get("auto"))
        want_exclusive = None
        if exclusive is not None and not is_auto:
            want_exclusive = bool(exclusive) and final_visibility in ("tokens", "owner")
            update_data["exclusive"] = want_exclusive
            update_data["searchable"] = bool(searchable) if want_exclusive else False

        try:
            result = await self.dbs["tracking"]["custom_catalogs"].update_one(
                {"_id": ObjectId(catalog_id)},
                {"$set": update_data}
            )
        except Exception:
            return False

        catalog = await self.dbs["tracking"]["custom_catalogs"].find_one({"_id": ObjectId(catalog_id)})
        items = catalog.get("items", []) if catalog else []

        #----- Stamp visibility onto the underlying media documents so the default
        #----- Latest/Popular catalogs and search honour it too
        if cascade:
            await self._apply_visibility_to_docs(items, final_visibility, tokens)

        #----- Apply/clear exclusivity on the documents and purge from every other catalog
        if want_exclusive is True:
            await self._apply_exclusivity_to_docs(items, catalog_id, bool(searchable))
            await self.purge_items_from_other_catalogs(catalog_id, items)
        elif want_exclusive is False:
            await self._clear_exclusivity_from_docs(items)

        return result.modified_count > 0

    #----- Stamp visibility onto media documents referenced by the given catalog items
    async def _apply_visibility_to_docs(self, items: List[dict], visibility: str, allowed_tokens: List[str]) -> None:
        groups: Dict[Tuple[int, str], List[int]] = {}
        for it in items or []:
            try:
                db_index = int(it.get("db_index", 1))
                collection = self._collection_for(it.get("media_type", "movie"))
                groups.setdefault((db_index, collection), []).append(int(it.get("tmdb_id")))
            except (TypeError, ValueError):
                continue
        for (db_index, collection), ids in groups.items():
            db_key = f"storage_{db_index}"
            if db_key not in self.dbs:
                continue
            try:
                #----- Metadata-only change: never touch updated_on (keeps Latest order)
                await self.dbs[db_key][collection].update_many(
                    {"tmdb_id": {"$in": ids}},
                    {"$set": {"visibility": visibility, "allowed_tokens": allowed_tokens}},
                )
            except Exception as e:
                LOGGER.error(f"_apply_visibility_to_docs failed for {db_key}.{collection}: {e}")

    #----- Group catalog items into {(db_index, collection): [tmdb_id, ...]}
    def _group_items_by_storage(self, items: List[dict]) -> Dict[Tuple[int, str], List[int]]:
        groups: Dict[Tuple[int, str], List[int]] = {}
        for it in items or []:
            try:
                db_index = int(it.get("db_index", 1))
                collection = self._collection_for(it.get("media_type", "movie"))
                groups.setdefault((db_index, collection), []).append(int(it.get("tmdb_id")))
            except (TypeError, ValueError):
                continue
        return groups

    #----- Lock the given titles to a single catalog (source of truth on the docs)
    async def _apply_exclusivity_to_docs(self, items: List[dict], catalog_id: str, searchable: bool) -> None:
        for (db_index, collection), ids in self._group_items_by_storage(items).items():
            db_key = f"storage_{db_index}"
            if db_key not in self.dbs:
                continue
            try:
                #----- Metadata-only change: never touch updated_on (keeps Latest order)
                await self.dbs[db_key][collection].update_many(
                    {"tmdb_id": {"$in": ids}},
                    {"$set": {"exclusive_catalog_id": str(catalog_id), "exclusive_searchable": bool(searchable)}},
                )
            except Exception as e:
                LOGGER.error(f"_apply_exclusivity_to_docs failed for {db_key}.{collection}: {e}")

    #----- Unlock the given titles so they return to default/auto/other catalogs
    async def _clear_exclusivity_from_docs(self, items: List[dict]) -> None:
        for (db_index, collection), ids in self._group_items_by_storage(items).items():
            db_key = f"storage_{db_index}"
            if db_key not in self.dbs:
                continue
            try:
                #----- Unlocking is metadata-only: keep updated_on so titles slot back into
                #----- their original place in Latest (auto.synced reset lets sync re-add)
                await self.dbs[db_key][collection].update_many(
                    {"tmdb_id": {"$in": ids}},
                    {"$unset": {"exclusive_catalog_id": "", "exclusive_searchable": ""},
                     "$set": {"auto_catalog.synced": False}},
                )
            except Exception as e:
                LOGGER.error(f"_clear_exclusivity_from_docs failed for {db_key}.{collection}: {e}")

    #----- Remove the given titles from every catalog except the one that owns them
    async def purge_items_from_other_catalogs(self, catalog_id: str, items: List[dict]) -> None:
        ids_by_type: Dict[str, set] = {}
        for it in items or []:
            try:
                ids_by_type.setdefault(self._collection_for(it.get("media_type", "movie")), set()).add(int(it.get("tmdb_id")))
            except (TypeError, ValueError):
                continue
        if not ids_by_type:
            return
        coll = self.dbs["tracking"]["custom_catalogs"]
        now = datetime.utcnow()
        for media_type, ids in ids_by_type.items():
            id_list = list(ids)
            try:
                await coll.update_many(
                    {"_id": {"$ne": ObjectId(catalog_id)},
                     "items": {"$elemMatch": {"tmdb_id": {"$in": id_list}, "media_type": media_type}}},
                    {"$pull": {"items": {"tmdb_id": {"$in": id_list}, "media_type": media_type}},
                     "$set": {"updated_at": now}},
                )
            except Exception as e:
                LOGGER.error(f"purge_items_from_other_catalogs failed: {e}")

    #----- Mark a single freshly-added title exclusive to its catalog
    async def mark_item_exclusive(self, catalog_id: str, tmdb_id: int, db_index: int, media_type: str, searchable: bool) -> None:
        item = {"tmdb_id": int(tmdb_id), "db_index": int(db_index), "media_type": self._collection_for(media_type)}
        await self._apply_exclusivity_to_docs([item], catalog_id, searchable)
        await self.purge_items_from_other_catalogs(catalog_id, [item])

    #----- Clear exclusivity for a single title (e.g. removed from its catalog)
    async def clear_item_exclusive(self, tmdb_id: int, db_index: int, media_type: str) -> None:
        item = {"tmdb_id": int(tmdb_id), "db_index": int(db_index), "media_type": self._collection_for(media_type)}
        await self._clear_exclusivity_from_docs([item])

    #----- Override one title's visibility inside a single catalog
    async def set_catalog_item_visibility(
        self, catalog_id: str, tmdb_id: int, db_index: int, media_type: str,
        visibility: str, allowed_tokens: Optional[List[str]] = None,
    ) -> bool:
        if visibility not in ("public", "tokens", "owner"):
            return False
        media_type = self._collection_for(media_type)
        try:
            result = await self.dbs["tracking"]["custom_catalogs"].update_one(
                {"_id": ObjectId(catalog_id),
                 "items": {"$elemMatch": {"tmdb_id": int(tmdb_id), "db_index": int(db_index), "media_type": media_type}}},
                {"$set": {
                    "items.$.visibility": visibility,
                    "items.$.allowed_tokens": list(allowed_tokens or []),
                    "updated_at": datetime.utcnow(),
                }},
            )
            return result.modified_count > 0
        except Exception:
            return False

    #----- Set a title's own visibility (source of truth for default + custom catalogs)
    async def set_media_visibility(
        self, tmdb_id: int, db_index: int, media_type: str,
        visibility: str, allowed_tokens: Optional[List[str]] = None,
    ) -> int:
        if visibility not in ("public", "tokens", "owner"):
            return 0
        tokens = list(allowed_tokens or [])
        collection = self._collection_for(media_type)
        now = datetime.utcnow()

        db_key = f"storage_{int(db_index)}"
        if db_key in self.dbs:
            try:
                #----- Metadata-only change: never touch updated_on (keeps Latest order)
                await self.dbs[db_key][collection].update_one(
                    {"tmdb_id": int(tmdb_id)},
                    {"$set": {"visibility": visibility, "allowed_tokens": tokens}},
                )
            except Exception as e:
                LOGGER.error(f"set_media_visibility doc update failed: {e}")

        #----- Keep any catalog items in sync so custom-catalog filtering matches
        try:
            result = await self.dbs["tracking"]["custom_catalogs"].update_many(
                {"items": {"$elemMatch": {"tmdb_id": int(tmdb_id), "db_index": int(db_index), "media_type": collection}}},
                {"$set": {
                    "items.$.visibility": visibility,
                    "items.$.allowed_tokens": tokens,
                    "updated_at": now,
                }},
            )
            return result.modified_count
        except Exception:
            return 0

    #----- A title's own visibility (from its media document)
    async def get_media_visibility(self, tmdb_id: int, db_index: int, media_type: str) -> Optional[dict]:
        doc = await self.get_document(media_type, int(tmdb_id), int(db_index))
        if not doc:
            return None
        return {
            "visibility": doc.get("visibility") or "public",
            "allowed_tokens": doc.get("allowed_tokens") or [],
        }

    async def delete_custom_catalog(self, catalog_id: str) -> bool:
        try:
            result = await self.dbs["tracking"]["custom_catalogs"].delete_one({"_id": ObjectId(catalog_id)})
            return result.deleted_count > 0
        except Exception:
            return False

    async def add_item_to_custom_catalog(
        self, catalog_id: str, tmdb_id: int, db_index: int, media_type: str
    ) -> bool:
        media_type = self._collection_for(media_type)
        doc = await self.get_document(media_type, int(tmdb_id), int(db_index))
        item = {
            "tmdb_id": int(tmdb_id),
            "db_index": int(db_index),
            "media_type": media_type,
            "added_at": datetime.utcnow(),
            "visibility": (doc.get("visibility") if doc else None) or "public",
            "allowed_tokens": (doc.get("allowed_tokens") if doc else None) or [],
        }
        try:
            result = await self.dbs["tracking"]["custom_catalogs"].update_one(
                {
                    "_id": ObjectId(catalog_id),
                    "items": {
                        "$not": {
                            "$elemMatch": {
                                "tmdb_id": int(tmdb_id),
                                "db_index": int(db_index),
                                "media_type": media_type,
                            }
                        }
                    },
                },
                {
                    "$push": {"items": {"$each": [item], "$position": 0}},
                    "$set": {"updated_at": datetime.utcnow()},
                }
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def remove_item_from_custom_catalog(
        self, catalog_id: str, tmdb_id: int, db_index: int, media_type: str
    ) -> bool:
        media_type = self._collection_for(media_type)
        try:
            result = await self.dbs["tracking"]["custom_catalogs"].update_one(
                {"_id": ObjectId(catalog_id)},
                {
                    "$pull": {
                        "items": {
                            "tmdb_id": int(tmdb_id),
                            "db_index": int(db_index),
                            "media_type": media_type,
                        }
                    },
                    "$set": {"updated_at": datetime.utcnow()},
                }
            )
            return result.modified_count > 0
        except Exception:
            return False

    async def find_media_doc(self, media_type: str, tmdb_id: int) -> Optional[Tuple[dict, int]]:
        #----- Locate a media doc by tmdb_id across storage DBs -> (doc, db_index) or None
        collection_name = self._collection_for(media_type)
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None

        for i in range(1, self.current_db_index + 1):
            db_key = f"storage_{i}"
            if db_key not in self.dbs:
                continue
            doc = await self.dbs[db_key][collection_name].find_one({"tmdb_id": tmdb_id})
            if doc:
                doc["db_index"] = i
                return doc, i
        return None

    async def purge_media_from_catalogs(self, tmdb_id: int, media_type: str) -> int:
        #----- Remove a media item from every catalog (auto + manual) by tmdb_id + media_type
        if tmdb_id in (None, "", 0):
            return 0
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return 0

        media_type = self._collection_for(media_type)
        collection = self.dbs["tracking"]["custom_catalogs"]
        now = datetime.utcnow()
        try:
            result = await collection.update_many(
                {"items": {"$elemMatch": {"tmdb_id": tmdb_id, "media_type": media_type}}},
                [
                    {
                        "$set": {
                            "items": {
                                "$filter": {
                                    "input": "$items",
                                    "as": "it",
                                    "cond": {
                                        "$not": [
                                            {
                                                "$and": [
                                                    {"$eq": ["$$it.tmdb_id", tmdb_id]},
                                                    {"$eq": ["$$it.media_type", media_type]},
                                                ]
                                            }
                                        ]
                                    },
                                }
                            }
                        }
                    },
                    {"$set": {"item_count": {"$size": "$items"}, "updated_at": now}},
                ],
            )
            if result.modified_count:
                LOGGER.info(
                    f"Purged {media_type} tmdb_id {tmdb_id} from "
                    f"{result.modified_count} catalog(s)."
                )
            return result.modified_count
        except Exception as e:
            LOGGER.error(f"Failed to purge tmdb_id {tmdb_id} from catalogs: {e}")
            return 0

    async def custom_catalog_contains_item(
        self, catalog_id: str, tmdb_id: int, db_index: int, media_type: str
    ) -> bool:
        media_type = self._collection_for(media_type)
        try:
            catalog = await self.dbs["tracking"]["custom_catalogs"].find_one({
                "_id": ObjectId(catalog_id),
                "items": {
                    "$elemMatch": {
                        "tmdb_id": int(tmdb_id),
                        "db_index": int(db_index),
                        "media_type": media_type,
                    }
                }
            })
            return bool(catalog)
        except Exception:
            return False

    async def get_custom_catalog_items(
        self, catalog_id: str, media_type: Optional[str] = None, page: int = 1, page_size: int = 24
    ) -> dict:
        catalog = await self.get_custom_catalog(catalog_id)
        if not catalog:
            return {"catalog": None, "items": [], "total_count": 0, "current_page": page, "total_pages": 0}

        db_media_type = None
        if media_type:
            db_media_type = self._collection_for(media_type)

        raw_items = catalog.get("items", []) or []
        if db_media_type:
            raw_items = [item for item in raw_items if item.get("media_type") == db_media_type]

        total_count = len(raw_items)
        skip = (page - 1) * page_size
        selected_items = raw_items[skip:skip + page_size]

        hydrated_items = await self.get_documents(selected_items)

        total_pages = (total_count + page_size - 1) // page_size if total_count else 0
        return {
            "catalog": catalog,
            "items": hydrated_items,
            "total_count": total_count,
            "current_page": page,
            "total_pages": total_pages,
        }


    #-----
    #----- Helper Methods for Repeated Logic
    #-----
    def _get_sort_dict(self, sort_params: List[Tuple[str, str]]) -> Dict[str, int]:
        if sort_params:
            sort_field, sort_direction = sort_params[0]
            return {sort_field: DESCENDING if sort_direction.lower() == "desc" else ASCENDING}
        return {"updated_on": DESCENDING}

    async def _paginate_collection(
        self,
        collection_name: str,
        sort_dict: Dict[str, int],
        page: int,
        page_size: int,
        filter_dict: Optional[dict] = None
    ):
        filter_dict = filter_dict or {}
        skip = (page - 1) * page_size
        results = []
        dbs_checked = []
        total_count = 0

        db_counts = []
        for i in range(1, self.current_db_index + 1):
            db_key = f"storage_{i}"
            db = self.dbs[db_key]
            count = await db[collection_name].count_documents(filter_dict)
            db_counts.append((i, count))
            total_count += count

        start_db_index = None
        for db_index, count in reversed(db_counts):
            if skip < count:
                start_db_index = db_index
                break
            skip -= count

        if not start_db_index:
            return [], [], total_count

        for db_index, count in reversed(db_counts):
            if db_index < start_db_index:
                continue

            db_key = f"storage_{db_index}"
            db = self.dbs[db_key]
            dbs_checked.append(db_index)

            cursor = (
                db[collection_name]
                .find(filter_dict)
                .sort(sort_dict)
                .skip(skip if db_index == start_db_index else 0)
                .limit(page_size - len(results))
            )

            docs = await cursor.to_list(None)
            results.extend(docs)

            if len(results) >= page_size:
                break

        return results, dbs_checked, total_count

    async def _move_document(
        self, collection_name: str, document: dict, old_db_index: int
    ) -> bool:
        current_db_key = f"storage_{self.current_db_index}"
        old_db_key = f"storage_{old_db_index}"
        document["db_index"] = self.current_db_index
        try:
            await self.dbs[current_db_key][collection_name].insert_one(document)
            await self.dbs[old_db_key][collection_name].delete_one({"_id": document["_id"]})
            LOGGER.info(f"✅ Moved document {document.get('tmdb_id')} from {old_db_key} to {current_db_key}")
            return True
        except Exception as e:
            LOGGER.error(f"Error moving document to {current_db_key}: {e}")
            return False

    async def _handle_storage_error(self, func, *args, total_storage_dbs: int) -> Optional[Any]:
        next_db_index = (self.current_db_index % total_storage_dbs) + 1
        if next_db_index == 1:
            LOGGER.warning("⚠️ All storage databases are full! Add more.")
            return None
        self.current_db_index = next_db_index
        await self.update_current_db_index()
        LOGGER.info(f"Switched to storage_{self.current_db_index}")
        return await func(*args)

    #----- Map any media_type spelling to its collection name
    @staticmethod
    def _collection_for(media_type: str) -> str:
        return "tv" if str(media_type).lower() in ("tv", "series") else "movie"

    #----- Load a doc by tmdb_id, apply an async mutator, and save only if it changed
    async def _edit_media_doc(self, collection_name: str, tmdb_id: int, db_index: int, mutate) -> bool:
        db_key = f"storage_{db_index}"
        doc = await self.dbs[db_key][collection_name].find_one({"tmdb_id": tmdb_id})
        if not doc:
            return False
        if not await mutate(doc):
            return False
        doc["updated_on"] = datetime.utcnow()
        result = await self.dbs[db_key][collection_name].replace_one({"tmdb_id": tmdb_id}, doc)
        return result.modified_count > 0

    #----- Locate an existing doc across storage DBs by imdb_id, then tmdb_id, then title+year
    async def _find_existing_media(
        self, collection_name: str, imdb_id, tmdb_id, title, release_year, total_storage_dbs: int
    ) -> Tuple[Optional[dict], Optional[str], Optional[int]]:
        for db_index in range(1, total_storage_dbs + 1):
            col = self.dbs[f"storage_{db_index}"][collection_name]
            doc = None
            if imdb_id:
                doc = await col.find_one({"imdb_id": imdb_id})
            if not doc and tmdb_id:
                doc = await col.find_one({"tmdb_id": tmdb_id})
            if not doc and title and release_year:
                doc = await col.find_one({"title": title, "release_year": release_year})
            if doc:
                return doc, f"storage_{db_index}", db_index
        return None, None, None


    #-----
    #----- Multi Database Method for insert/update/delete/list
    #-----

    async def _build_part_id_and_size(self, parts: List[dict]) -> Tuple[str, str]:
        sorted_parts = sorted(parts, key=lambda p: p.get("part_number", 0))
        payload = {"parts": [{"chat_id": p["chat_id"], "msg_id": p["msg_id"]} for p in sorted_parts]}
        encoded = await encode_string(payload)
        total_bytes = sum(p.get("size_bytes", 0) for p in sorted_parts)
        from Backend.helper.pyro import get_readable_file_size 
        size_str = get_readable_file_size(total_bytes)
        return encoded, size_str

    async def get_media_ids_by_part(
        self, channel: int, msg_id: int
    ) -> Optional[Tuple[Optional[str], Optional[int]]]:
        try:
            legacy_hash = await encode_string({"chat_id": channel, "msg_id": msg_id})
        except Exception:
            legacy_hash = None

        part_match = {"$elemMatch": {"chat_id": channel, "msg_id": msg_id}}
        projection = {"imdb_id": 1, "tmdb_id": 1}

        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]

            movie_or = [{"telegram.parts": part_match}]
            tv_or = [{"seasons.episodes.telegram.parts": part_match}]
            if legacy_hash:
                movie_or.append({"telegram.id": legacy_hash})
                tv_or.append({"seasons.episodes.telegram.id": legacy_hash})

            doc = await db["movie"].find_one({"$or": movie_or}, projection)
            if not doc:
                doc = await db["tv"].find_one({"$or": tv_or}, projection)
            if doc:
                return doc.get("imdb_id"), doc.get("tmdb_id")

        return None

    async def remove_media_part(self, channel: int, msg_id: int) -> bool:
        try:
            legacy_hash = await encode_string({"chat_id": channel, "msg_id": msg_id})
            if await self.delete_media_by_stream_id(legacy_hash):
                return True
        except Exception as e:
            LOGGER.error(f"remove_media_part: legacy lookup failed: {e}")

        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]

            movie = await db["movie"].find_one(
                {"telegram.parts": {"$elemMatch": {"chat_id": channel, "msg_id": msg_id}}}
            )
            if movie:
                new_telegram = []
                for q in movie.get("telegram", []):
                    parts = q.get("parts")
                    if parts and any(p.get("chat_id") == channel and p.get("msg_id") == msg_id for p in parts):
                        remaining = [p for p in parts if not (p.get("chat_id") == channel and p.get("msg_id") == msg_id)]
                        if not remaining:
                            continue  #----- last part removed: drop the whole quality entry
                        new_id, new_size = await self._build_part_id_and_size(remaining)
                        q["parts"] = remaining
                        q["id"] = new_id
                        q["size"] = new_size
                    new_telegram.append(q)

                if len(new_telegram) == 0:
                    await db["movie"].delete_one({"_id": movie["_id"]})
                    await self.purge_media_from_catalogs(movie.get("tmdb_id"), "movie")
                else:
                    movie["telegram"] = new_telegram
                    movie["updated_on"] = datetime.utcnow()
                    await db["movie"].replace_one({"_id": movie["_id"]}, movie)
                return True

            tv = await db["tv"].find_one(
                {"seasons.episodes.telegram.parts": {"$elemMatch": {"chat_id": channel, "msg_id": msg_id}}}
            )
            if tv:
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        new_telegram = []
                        for q in episode.get("telegram", []):
                            parts = q.get("parts")
                            if parts and any(p.get("chat_id") == channel and p.get("msg_id") == msg_id for p in parts):
                                remaining = [p for p in parts if not (p.get("chat_id") == channel and p.get("msg_id") == msg_id)]
                                if not remaining:
                                    continue
                                new_id, new_size = await self._build_part_id_and_size(remaining)
                                q["parts"] = remaining
                                q["id"] = new_id
                                q["size"] = new_size
                            new_telegram.append(q)
                        episode["telegram"] = new_telegram
                    season["episodes"] = [e for e in season.get("episodes", []) if e.get("telegram")]
                tv["seasons"] = [s for s in tv.get("seasons", []) if s.get("episodes")]

                if len(tv["seasons"]) == 0:
                    await db["tv"].delete_one({"_id": tv["_id"]})
                    await self.purge_media_from_catalogs(tv.get("tmdb_id"), "tv")
                else:
                    tv["updated_on"] = datetime.utcnow()
                    await db["tv"].replace_one({"_id": tv["_id"]}, tv)
                return True

        return False

    async def insert_media(
        self, metadata_info: dict,
        channel: int, msg_id: int, size: str, name: str, raw_size: int = 0
    ) -> Optional[ObjectId]:

        group_key = metadata_info.get("group_key")
        part_number = metadata_info.get("part_number")

        if group_key:
            part = {
                "part_number": part_number or 1,
                "chat_id": channel,
                "msg_id": msg_id,
                "size_bytes": raw_size,
            }
            part_id, part_size = await self._build_part_id_and_size([part])
            quality_detail = QualityDetail(
                quality=metadata_info['quality'],
                id=part_id,
                name=name,
                size=part_size,
                group_key=group_key,
                parts=[QualityPart(**part)],
            )
        else:
            quality_detail = QualityDetail(
                quality=metadata_info['quality'],
                id=metadata_info['encoded_string'],
                name=name,
                size=size,
            )

        if metadata_info['media_type'] == "movie":
            media = MovieSchema(
                tmdb_id=metadata_info['tmdb_id'],
                imdb_id=metadata_info['imdb_id'],
                db_index=self.current_db_index,
                title=metadata_info['title'],
                genres=metadata_info['genres'],
                description=metadata_info['description'],
                rating=metadata_info['rate'],
                release_year=metadata_info['year'],
                poster=metadata_info['poster'],
                backdrop=metadata_info['backdrop'],
                logo=metadata_info['logo'],
                cast=metadata_info['cast'],
                runtime=metadata_info['runtime'],
                media_type=metadata_info['media_type'],
                is_anime=metadata_info.get('is_anime', False),
                original_language=metadata_info.get('original_language'),
                origin_country=metadata_info.get('origin_country', []) or [],
                telegram=[quality_detail]
            )
            return await self.update_movie(media)
        else:
            tv_show = TVShowSchema(
                tmdb_id=metadata_info['tmdb_id'],
                imdb_id=metadata_info['imdb_id'],
                db_index=self.current_db_index,
                title=metadata_info['title'],
                genres=metadata_info['genres'],
                description=metadata_info['description'],
                rating=metadata_info['rate'],
                release_year=metadata_info['year'],
                poster=metadata_info['poster'],
                backdrop=metadata_info['backdrop'],
                logo=metadata_info['logo'],
                cast=metadata_info['cast'],
                runtime=metadata_info['runtime'],
                media_type=metadata_info['media_type'],
                is_anime=metadata_info.get('is_anime', False),
                original_language=metadata_info.get('original_language'),
                origin_country=metadata_info.get('origin_country', []) or [],
                seasons=[Season(
                    season_number=metadata_info['season_number'],
                    episodes=[Episode(
                        episode_number=metadata_info['episode_number'],
                        title=metadata_info['episode_title'],
                        episode_backdrop=metadata_info['episode_backdrop'],
                        overview=metadata_info['episode_overview'],
                        released=metadata_info['episode_released'],
                        telegram=[quality_detail]
                    )]
                )]
            )
            return await self.update_tv_show(tv_show)

    async def _delete_split_part(self, part: dict) -> None:
        try:
            chat_id = int(f"-100{part['chat_id']}")
            msg_id = int(part["msg_id"])
            create_task(delete_message(chat_id, msg_id))
        except Exception as e:
            LOGGER.error(f"Failed to delete split part message: {e}")

    async def _merge_split_part(self, qualities: List[dict], quality_to_update: dict) -> List[dict]:
        group_key = quality_to_update.get("group_key")
        incoming_parts = quality_to_update.get("parts") or []
        if not incoming_parts:
            return qualities + [quality_to_update]
        new_part = incoming_parts[0]
        replace_mode = SettingsManager.current().replace_mode

        merged = False
        result = []
        for q in qualities:
            if group_key is not None and q.get("group_key") == group_key:
                existing_parts = q.get("parts") or []

                if replace_mode:
                    for old_part in existing_parts:
                        if old_part.get("part_number") != new_part.get("part_number"):
                            continue
                        if (
                            old_part.get("chat_id") == new_part.get("chat_id")
                            and old_part.get("msg_id") == new_part.get("msg_id")
                        ):
                            continue
                        await self._delete_split_part(old_part)

                existing_parts = [
                    p for p in existing_parts if p.get("part_number") != new_part.get("part_number")
                ]
                existing_parts.append(new_part)
                new_id, new_size = await self._build_part_id_and_size(existing_parts)
                q["parts"] = existing_parts
                q["id"] = new_id
                q["size"] = new_size
                q["name"] = quality_to_update.get("name", q.get("name"))
                merged = True
            result.append(q)

        if not merged:
            result.append(quality_to_update)
        return result

    #----- Identity of a non-split stream for duplicate protection (quality + name + size)
    @staticmethod
    def _dup_key(quality: dict) -> tuple:
        name = re.sub(r"\s+", " ", str(quality.get("name") or "").strip().lower())
        size = str(quality.get("size") or "").strip().lower()
        return (quality.get("quality"), name, size)

    async def _apply_quality_update(
        self, existing_qualities: List[dict], quality_to_update: dict
    ) -> List[dict]:
        target_quality = quality_to_update.get("quality")
        incoming_group_key = quality_to_update.get("group_key")
        replace_mode = SettingsManager.current().replace_mode

        if incoming_group_key:
            #----- Incoming is a split part.
            if replace_mode:
                stale = [
                    q for q in existing_qualities
                    if q.get("quality") == target_quality
                    and q.get("group_key") != incoming_group_key
                ]
                for q in stale:
                    await self._queue_quality_deletion(q)
                existing_qualities = [
                    q for q in existing_qualities
                    if not (
                        q.get("quality") == target_quality
                        and q.get("group_key") != incoming_group_key
                    )
                ]
            return await self._merge_split_part(existing_qualities, quality_to_update)

        #----- Incoming is a normal (non-split) file.
        if replace_mode:
            stale = [q for q in existing_qualities if q.get("quality") == target_quality]
            for q in stale:
                await self._queue_quality_deletion(q)
            existing_qualities = [
                q for q in existing_qualities if q.get("quality") != target_quality
            ]
            existing_qualities.append(quality_to_update)
            return existing_qualities

        #----- REPLACE_MODE off: skip exact duplicates when protection is on, else stack.
        if SettingsManager.current().duplicate_protection:
            key = self._dup_key(quality_to_update)
            for q in existing_qualities:
                if not q.get("group_key") and self._dup_key(q) == key:
                    LOGGER.info(f"Duplicate protection: skipped existing stream '{quality_to_update.get('name')}'.")
                    return existing_qualities
        existing_qualities.append(quality_to_update)
        return existing_qualities

    async def update_movie(self, movie_data: MovieSchema) -> Optional[ObjectId]:
        try:
            movie_dict = movie_data.dict()
        except ValidationError as e:
            LOGGER.error(f"Validation error: {e}")
            return None

        imdb_id = movie_dict["imdb_id"]
        tmdb_id = movie_dict["tmdb_id"]
        title = movie_dict["title"]
        release_year = movie_dict["release_year"]

        quality_to_update = movie_dict["telegram"][0]

        current_db_key = f"storage_{self.current_db_index}"
        total_storage_dbs = len(self.dbs) - 1

        existing_movie, existing_db_key, existing_db_index = await self._find_existing_media(
            "movie", imdb_id, tmdb_id, title, release_year, total_storage_dbs
        )

        #----- INSERT NEW MOVIE ----------------
        if not existing_movie:
            try:
                movie_dict["db_index"] = self.current_db_index
                result = await self.dbs[current_db_key]["movie"].insert_one(movie_dict)
                return result.inserted_id
            except Exception as e:
                LOGGER.error(f"Insertion failed in {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_movie, movie_data, total_storage_dbs=total_storage_dbs)
                return None

        #----- UPDATE MOVIE ----------------
        movie_id = existing_movie["_id"]

        if imdb_id and not existing_movie.get("imdb_id"):
            existing_movie["imdb_id"] = imdb_id
        if tmdb_id and not existing_movie.get("tmdb_id"):
            existing_movie["tmdb_id"] = tmdb_id
        if movie_dict.get("is_anime"):
            existing_movie["is_anime"] = True

        existing_qualities = existing_movie.get("telegram", [])

        existing_qualities = await self._apply_quality_update(existing_qualities, quality_to_update)

        existing_movie["telegram"] = existing_qualities
        existing_movie["updated_on"] = datetime.utcnow()

        if existing_db_index != self.current_db_index:
            try:
                if await self._move_document("movie", existing_movie, existing_db_index):
                    return movie_id
            except Exception as e:
                LOGGER.error(f"Error moving movie to {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_movie, movie_data, total_storage_dbs=total_storage_dbs)

        try:
            await self.dbs[existing_db_key]["movie"].replace_one({"_id": movie_id}, existing_movie)
            return movie_id
        except Exception as e:
            LOGGER.error(f"Failed to update movie {tmdb_id} in {existing_db_key}: {e}")
            if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                return await self._handle_storage_error(self.update_movie, movie_data, total_storage_dbs=total_storage_dbs)

    async def update_tv_show(self, tv_show_data: TVShowSchema) -> Optional[ObjectId]:
        try:
            tv_show_dict = tv_show_data.dict()
        except ValidationError as e:
            LOGGER.error(f"Validation error: {e}")
            return None

        imdb_id = tv_show_dict.get("imdb_id")
        tmdb_id = tv_show_dict.get("tmdb_id")
        title = tv_show_dict["title"]
        release_year = tv_show_dict["release_year"]

        current_db_key = f"storage_{self.current_db_index}"
        total_storage_dbs = len(self.dbs) - 1

        existing_tv, existing_db_key, existing_db_index = await self._find_existing_media(
            "tv", imdb_id, tmdb_id, title, release_year, total_storage_dbs
        )

        #----- INSERT NEW TV ----------------
        if not existing_tv:
            try:
                tv_show_dict["db_index"] = self.current_db_index
                result = await self.dbs[current_db_key]["tv"].insert_one(tv_show_dict)
                return result.inserted_id
            except Exception as e:
                LOGGER.error(f"Insertion failed in {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_tv_show, tv_show_data, total_storage_dbs=total_storage_dbs)
                return None

        #----- UPDATE TV ----------------
        tv_id = existing_tv["_id"]

        if imdb_id and not existing_tv.get("imdb_id"):
            existing_tv["imdb_id"] = imdb_id
        if tmdb_id and not existing_tv.get("tmdb_id"):
            existing_tv["tmdb_id"] = tmdb_id
        if tv_show_dict.get("is_anime"):
            existing_tv["is_anime"] = True

        for season in tv_show_dict["seasons"]:
            existing_season = next(
                (s for s in existing_tv["seasons"]
                if s["season_number"] == season["season_number"]),
                None
            )

            if not existing_season:
                existing_tv["seasons"].append(season)
                continue

            for episode in season["episodes"]:
                existing_episode = next(
                    (e for e in existing_season["episodes"]
                    if e["episode_number"] == episode["episode_number"]),
                    None
                )

                if not existing_episode:
                    existing_season["episodes"].append(episode)
                    continue

                existing_episode.setdefault("telegram", [])

                for quality in episode["telegram"]:
                    existing_episode["telegram"] = await self._apply_quality_update(
                        existing_episode["telegram"], quality
                    )

        existing_tv["updated_on"] = datetime.utcnow()

        #----- MOVE DB IF NEEDED ----------------
        if existing_db_index != self.current_db_index:
            try:
                if await self._move_document("tv", existing_tv, existing_db_index):
                    return tv_id
            except Exception as e:
                LOGGER.error(f"Error moving TV show to {current_db_key}: {e}")
                if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                    return await self._handle_storage_error(self.update_tv_show, tv_show_data, total_storage_dbs=total_storage_dbs)
            return tv_id

        try:
            await self.dbs[existing_db_key]["tv"].replace_one({"_id": tv_id}, existing_tv)
            return tv_id
        except Exception as e:
            LOGGER.error(f"Failed to update TV show {tmdb_id} in {existing_db_key}: {e}")
            if any(keyword in str(e).lower() for keyword in ["storage", "quota"]):
                return await self._handle_storage_error(self.update_tv_show, tv_show_data, total_storage_dbs=total_storage_dbs)
    
    async def sort_movies(self, sort_params, page, page_size, genre_filter=None, extra_filter=None):
        sort_dict = self._get_sort_dict(sort_params)
        filter_dict = {"genres": {"$in": [genre_filter]}} if genre_filter else {}
        if extra_filter:
            filter_dict.update(extra_filter)
        results, dbs_checked, total_count = await self._paginate_collection(
            "movie", sort_dict, page, page_size, filter_dict=filter_dict
        )
        total_pages = (total_count + page_size - 1) // page_size
        return {
            "total_count": total_count,
            "total_pages": total_pages,
            "databases_checked": dbs_checked,
            "current_page": page,
            "movies": [convert_objectid_to_str(result) for result in results],
        }

    async def sort_tv_shows(self, sort_params, page, page_size, genre_filter=None, extra_filter=None):
        sort_dict = self._get_sort_dict(sort_params)
        filter_dict = {"genres": {"$in": [genre_filter]}} if genre_filter else {}
        if extra_filter:
            filter_dict.update(extra_filter)
        results, dbs_checked, total_count = await self._paginate_collection(
            "tv", sort_dict, page, page_size, filter_dict=filter_dict
        )
        total_pages = (total_count + page_size - 1) // page_size
        return {
            "total_count": total_count,
            "total_pages": total_pages,
            "databases_checked": dbs_checked,
            "current_page": page,
            "tv_shows": [convert_objectid_to_str(result) for result in results],
        }

    async def search_documents(
            self, 
            query: str, 
            page: int, 
            page_size: int,
            extra_filter: Optional[dict] = None
        ) -> dict:

            skip = (page - 1) * page_size
            
            words = query.split()
            regex_query = {
                '$regex': '.*' + '.*'.join(words) + '.*', 
                '$options': 'i'
            }

            tv_match = {"$or": [
                {"title": regex_query},
                {"seasons.episodes.telegram.name": regex_query}
            ]}
            movie_match = {"$or": [
                {"title": regex_query},
                {"telegram.name": regex_query}
            ]}
            if extra_filter:
                tv_match = {"$and": [tv_match, extra_filter]}
                movie_match = {"$and": [movie_match, extra_filter]}

            tv_pipeline = [
                {"$match": tv_match},
                {"$project": {
                    "_id": 1, "tmdb_id": 1, "title": 1, "genres": 1, "rating": 1, "imdb_id": 1,
                    "release_year": 1, "poster": 1, "backdrop": 1, "description": 1, "logo": 1,
                    "media_type": 1, "db_index": 1
                }}
            ]
            
            movie_pipeline = [
                {"$match": movie_match},
                {"$project": {
                    "_id": 1, "tmdb_id": 1, "title": 1, "genres": 1, "rating": 1,
                    "release_year": 1, "poster": 1, "backdrop": 1, "description": 1,
                    "media_type": 1, "db_index": 1, "imdb_id": 1, "logo": 1
                }}
            ]
            
            results = []
            dbs_checked = []
            
            active_db_key = f"storage_{self.current_db_index}"
            active_db = self.dbs[active_db_key]
            dbs_checked.append(self.current_db_index)
            
            tv_results = await active_db["tv"].aggregate(tv_pipeline).to_list(None)
            movie_results = await active_db["movie"].aggregate(movie_pipeline).to_list(None)
            combined = tv_results + movie_results
            results.extend(combined)
            
            if len(results) < page_size:
                previous_db_index = self.current_db_index - 1
                while previous_db_index > 0 and len(results) < page_size:
                    prev_db_key = f"storage_{previous_db_index}"
                    prev_db = self.dbs[prev_db_key]
                    tv_results_prev = await prev_db["tv"].aggregate(tv_pipeline).to_list(None)
                    movie_results_prev = await prev_db["movie"].aggregate(movie_pipeline).to_list(None)
                    combined_prev = tv_results_prev + movie_results_prev
                    results.extend(combined_prev)
                    dbs_checked.append(previous_db_index)
                    previous_db_index -= 1

            total_count = 0
            for db_index in dbs_checked:
                key = f"storage_{db_index}"
                db = self.dbs[key]
                tv_count = await db["tv"].count_documents(tv_match)
                movie_count = await db["movie"].count_documents(movie_match)
                total_count += (tv_count + movie_count)
            
            paged_results = results[skip:skip + page_size]

            return {
                "total_count": total_count,
                "results": [convert_objectid_to_str(doc) for doc in paged_results]
            }


    async def get_media_details(
        self, 
        imdb_id: str,
        season_number: Optional[int] = None, 
        episode_number: Optional[int] = None
    ) -> Optional[dict]:

        for db_idx in range(self.current_db_index, 0, -1):
            db_key = f"storage_{db_idx}"
            
            if episode_number is not None and season_number is not None:
                tv_show = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
                if tv_show:
                    for season in tv_show.get("seasons", []):
                        if season.get("season_number") == season_number:
                            for episode in season.get("episodes", []):
                                if episode.get("episode_number") == episode_number:
                                    details = convert_objectid_to_str(episode)
                                    details.update({
                                        "imdb_id": imdb_id,
                                        "type": "tv",
                                        "season_number": season_number,
                                        "episode_number": episode_number,
                                        "backdrop": episode.get("episode_backdrop"),
                                        "db_index": db_idx
                                    })
                                    return details
            
            elif season_number is not None:
                tv_show = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
                if tv_show:
                    for season in tv_show.get("seasons", []):
                        if season.get("season_number") == season_number:
                            details = convert_objectid_to_str(season)
                            details.update({
                                "imdb_id": imdb_id,
                                "type": "tv",
                                "season_number": season_number,
                                "db_index": db_idx
                            })
                            return details
            
            else:
                tv_doc = await self.dbs[db_key]["tv"].find_one({"imdb_id": imdb_id})
                if tv_doc:
                    tv_doc = convert_objectid_to_str(tv_doc)
                    tv_doc["type"] = "tv"
                    tv_doc["db_index"] = db_idx
                    return tv_doc
                
                movie_doc = await self.dbs[db_key]["movie"].find_one({"imdb_id": imdb_id})
                if movie_doc:
                    movie_doc = convert_objectid_to_str(movie_doc)
                    movie_doc["type"] = "movie"
                    movie_doc["db_index"] = db_idx
                    return movie_doc
        
        return None

    #-----
    #----- DB Method for Edit Post
    #-----

    async def get_document(self, media_type: str, tmdb_id: int, db_index: int) -> Optional[Dict[str, Any]]:
        db_key = f"storage_{db_index}"
        collection_name = self._collection_for(media_type)
        document = await self.dbs[db_key][collection_name].find_one({"tmdb_id": int(tmdb_id)})
        return convert_objectid_to_str(document) if document else None

    async def get_documents(self, refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not refs:
            return []

        groups: Dict[Tuple[int, str], List[int]] = {}
        normalized: List[Tuple[int, str, int]] = []
        for ref in refs:
            try:
                tmdb_id = int(ref.get("tmdb_id"))
                db_index = int(ref.get("db_index", 1))
            except (TypeError, ValueError):
                continue
            collection_name = self._collection_for(ref.get("media_type", "movie"))
            groups.setdefault((db_index, collection_name), []).append(tmdb_id)
            normalized.append((db_index, collection_name, tmdb_id))

        lookup: Dict[Tuple[int, str, int], Dict[str, Any]] = {}
        for (db_index, collection_name), ids in groups.items():
            db_key = f"storage_{db_index}"
            if db_key not in self.dbs:
                continue
            try:
                cursor = self.dbs[db_key][collection_name].find({"tmdb_id": {"$in": ids}})
                async for document in cursor:
                    document = convert_objectid_to_str(document)
                    try:
                        key = (db_index, collection_name, int(document.get("tmdb_id")))
                    except (TypeError, ValueError):
                        continue
                    lookup[key] = document
            except Exception as e:
                LOGGER.error(f"get_documents batch fetch failed for {db_key}/{collection_name}: {e}")

        ordered: List[Dict[str, Any]] = []
        for key in normalized:
            document = lookup.get(key)
            if document is not None:
                ordered.append(document)
        return ordered

    async def update_document(
        self, media_type: str, tmdb_id: int, db_index: int, update_data: Dict[str, Any]
    ):
        update_data.pop('_id', None)
        db_key = f"storage_{db_index}"
        collection_name = self._collection_for(media_type)
        collection = self.dbs[db_key][collection_name]

        try:
            result = await collection.update_one({"tmdb_id": int(tmdb_id)}, {"$set": update_data})

            return result.modified_count > 0

        except Exception as e:
            err_str = str(e).lower()
            LOGGER.error(f"Error updating document in {db_key}: {e}")
            if "storage" in err_str or "quota" in err_str:
                total_storage_dbs = len(self.dbs) - 1
                db_index_int = int(db_index)
                next_db_index = (db_index_int % total_storage_dbs) + 1
                if next_db_index == 1:
                    LOGGER.warning("⚠️ All storage databases are full! Add more.")
                    return False

                new_db_key = f"storage_{next_db_index}"
                LOGGER.info(f"Switching from {db_key} to {new_db_key} due to storage error.")

                try:
                    old_doc = await self.dbs[db_key][collection_name].find_one({"tmdb_id": int(tmdb_id)})
                    if not old_doc:
                        LOGGER.error(f"Document with tmdb_id {tmdb_id} not found in {db_key} during migration.")
                        return False

                    old_doc.update(update_data)
                    old_doc["db_index"] = next_db_index
                    old_doc.pop("_id", None)
                    insert_result = await self.dbs[new_db_key][collection_name].insert_one(old_doc)
                    LOGGER.info(f"Inserted document {insert_result.inserted_id} into {new_db_key}")
                    await self.dbs[db_key][collection_name].delete_one({"tmdb_id": int(tmdb_id)})
                    LOGGER.info(f"Deleted document tmdb_id {tmdb_id} from {db_key}")
                    self.current_db_index = next_db_index
                    await self.update_current_db_index()
                    LOGGER.info(f"Switched to {new_db_key} and document migrated successfully.")
                    return True

                except Exception as migrate_error:
                    LOGGER.error(f"Error migrating document tmdb_id {tmdb_id} to {new_db_key}: {migrate_error}")
                    return False
            raise

    #----- Queue deletion of the Telegram message(s) backing a quality (split or single)
    async def _queue_quality_deletion(self, quality: dict) -> None:
        parts = quality.get("parts")
        if parts:
            for part in parts:
                await self._delete_split_part(part)
            return

        old_id = quality.get("id")
        if not old_id:
            return
        try:
            decoded = await decode_string(old_id)
            if isinstance(decoded, dict) and decoded.get("parts"):
                for part in decoded["parts"]:
                    await self._delete_split_part(part)
                return
            chat_id = int(f"-100{decoded['chat_id']}")
            msg_id = int(decoded["msg_id"])
            create_task(delete_message(chat_id, msg_id))
        except Exception as e:
            LOGGER.error(f"Failed to queue file for deletion: {e}")

    async def delete_document(self, media_type: str, tmdb_id: int, db_index: int) -> bool:
        db_key = f"storage_{db_index}"
        collection_name = self._collection_for(media_type)

        doc = await self.dbs[db_key][collection_name].find_one({"tmdb_id": tmdb_id})
        if doc:
            if collection_name == "movie":
                for quality in doc.get("telegram", []):
                    await self._queue_quality_deletion(quality)
            else:
                for season in doc.get("seasons", []):
                    for episode in season.get("episodes", []):
                        for quality in episode.get("telegram", []):
                            await self._queue_quality_deletion(quality)

        result = await self.dbs[db_key][collection_name].delete_one({"tmdb_id": tmdb_id})
        if result.deleted_count > 0:
            await self.purge_media_from_catalogs(tmdb_id, collection_name)
            LOGGER.info(f"{media_type} with tmdb_id {tmdb_id} deleted successfully.")
            return True
        LOGGER.info(f"No document found with tmdb_id {tmdb_id}.")
        return False

    async def get_title_by_stream_id(self, stream_id_hash: str) -> Optional[str]:
        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]
            
            #----- Check Movies
            movie = await db["movie"].find_one({"telegram.id": stream_id_hash})
            if movie and "telegram" in movie:
                for t in movie["telegram"]:
                    if t.get("id") == stream_id_hash:
                        return movie.get("title")

            #----- Check TV Shows
            tv = await db["tv"].find_one({"seasons.episodes.telegram.id": stream_id_hash})
            if tv and "seasons" in tv:
                title = tv.get("title", "Unknown Series")
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        for t in episode.get("telegram", []):
                            if t.get("id") == stream_id_hash:
                                s_num = season.get("season_number", 0)
                                e_num = episode.get("episode_number", 0)
                                return f"{title} S{s_num:02d}E{e_num:02d}"

        return None

    async def delete_media_by_stream_id(self, stream_id_hash: str, delete_file: bool = False) -> bool:
        for i in range(1, self.current_db_index + 1):
            db = self.dbs[f"storage_{i}"]
            
            #----- Check Movies
            movie = await db["movie"].find_one({"telegram.id": stream_id_hash})
            if movie:
                if delete_file:
                    for q in movie.get("telegram", []):
                        if q.get("id") == stream_id_hash:
                            await self._queue_quality_deletion(q)
                            break
                movie["telegram"] = [q for q in movie.get("telegram", []) if q.get("id") != stream_id_hash]
                if len(movie["telegram"]) == 0:
                    await db["movie"].delete_one({"_id": movie["_id"]})
                    await self.purge_media_from_catalogs(movie.get("tmdb_id"), "movie")
                else:
                    movie['updated_on'] = datetime.utcnow()
                    await db["movie"].replace_one({"_id": movie["_id"]}, movie)
                return True

            #----- Check TV Shows
            tv = await db["tv"].find_one({"seasons.episodes.telegram.id": stream_id_hash})
            if tv:
                for season in tv.get("seasons", []):
                    for episode in season.get("episodes", []):
                        for q in episode.get("telegram", []):
                            if q.get("id") == stream_id_hash:
                                if delete_file:
                                    await self._queue_quality_deletion(q)
                                episode["telegram"] = [t for t in episode.get("telegram", []) if t.get("id") != stream_id_hash]
                                if len(episode["telegram"]) == 0:
                                    season["episodes"] = [e for e in season.get("episodes", []) if e.get("episode_number") != episode.get("episode_number")]
                                    if len(season["episodes"]) == 0:
                                        tv["seasons"] = [s for s in tv.get("seasons", []) if s.get("season_number") != season.get("season_number")]
                                        if len(tv["seasons"]) == 0:
                                            await db["tv"].delete_one({"_id": tv["_id"]})
                                            await self.purge_media_from_catalogs(tv.get("tmdb_id"), "tv")
                                            return True
                                tv['updated_on'] = datetime.utcnow()
                                await db["tv"].replace_one({"_id": tv["_id"]}, tv)
                                return True
        return False

    async def delete_movie_quality(self, tmdb_id: int, db_index: int, id: str) -> bool:
        async def mutate(movie):
            qualities = movie.get("telegram") or []
            for q in qualities:
                if q.get("id") == id:
                    await self._queue_quality_deletion(q)
                    break
            original_len = len(qualities)
            movie["telegram"] = [q for q in qualities if q.get("id") != id]
            return len(movie["telegram"]) != original_len
        return await self._edit_media_doc("movie", tmdb_id, db_index, mutate)

    async def delete_tv_quality(self, tmdb_id: int, db_index: int, season_number: int, episode_number: int, id: str) -> bool:
        async def mutate(tv):
            for season in tv.get("seasons", []):
                if season.get("season_number") != season_number:
                    continue
                for episode in season.get("episodes", []):
                    if episode.get("episode_number") == episode_number and "telegram" in episode:
                        for q in episode["telegram"]:
                            if q.get("id") == id:
                                await self._queue_quality_deletion(q)
                                break
                        original_len = len(episode["telegram"])
                        episode["telegram"] = [q for q in episode["telegram"] if q.get("id") != id]
                        return original_len > len(episode["telegram"])
            return False
        return await self._edit_media_doc("tv", tmdb_id, db_index, mutate)

    async def delete_tv_episode(self, tmdb_id: int, db_index: int, season_number: int, episode_number: int) -> bool:
        async def mutate(tv):
            for season in tv.get("seasons", []):
                if season.get("season_number") != season_number:
                    continue
                episodes = season.get("episodes", [])
                for ep in episodes:
                    if ep.get("episode_number") == episode_number:
                        for quality in ep.get("telegram", []):
                            await self._queue_quality_deletion(quality)
                        break
                original_len = len(episodes)
                season["episodes"] = [ep for ep in episodes if ep.get("episode_number") != episode_number]
                return original_len > len(season["episodes"])
            return False
        return await self._edit_media_doc("tv", tmdb_id, db_index, mutate)

    async def delete_tv_season(self, tmdb_id: int, db_index: int, season_number: int) -> bool:
        async def mutate(tv):
            seasons = tv.get("seasons", [])
            for season in seasons:
                if season.get("season_number") == season_number:
                    for episode in season.get("episodes", []):
                        for quality in episode.get("telegram", []):
                            await self._queue_quality_deletion(quality)
                    break
            original_len = len(seasons)
            tv["seasons"] = [s for s in seasons if s.get("season_number") != season_number]
            return len(tv["seasons"]) != original_len
        return await self._edit_media_doc("tv", tmdb_id, db_index, mutate)


    #----- Get per-DB statistics (movies, tv shows, used size, etc.)
    async def get_database_stats(self):
        stats = []
        for key in self.dbs.keys():
            if key.startswith("storage_"):
                db = self.dbs[key]
                movie_count = await db["movie"].count_documents({})
                tv_count = await db["tv"].count_documents({})
                db_stats = await db.command("dbstats")
                stats.append({
                    "db_name": key,
                    "movie_count": movie_count,
                    "tv_count": tv_count,
                    "storageSize": db_stats.get("storageSize", 0),
                    "dataSize": db_stats.get("dataSize", 0)
                })
        return stats



    #-----
    #----- API Token Methods
    #-----

    async def add_api_token(self, name: str, daily_limit_gb: float = None, monthly_limit_gb: float = None, user_id: int = None, subscription_exempt: bool = False) -> dict:
        #----- If a user_id is provided, return existing token if already created
        if user_id:
            existing = await self.dbs["tracking"]["api_tokens"].find_one({"user_id": user_id})
            if existing:
                return convert_objectid_to_str(existing)

        alphabet = string.ascii_letters + string.digits
        token = ''.join(secrets.choice(alphabet) for _ in range(32))

        token_doc = {
            "name": name,
            "token": token,
            "user_id": user_id,
            "is_admin": self._is_owner(user_id),
            "subscription_exempt": bool(subscription_exempt),
            "expires_at": None,
            "created_at": datetime.utcnow(),
            "limits": {
                "daily_limit_gb": daily_limit_gb if daily_limit_gb else 0,
                "monthly_limit_gb": monthly_limit_gb if monthly_limit_gb else 0
            },
            "usage": {
                "total_bytes": 0,
                "daily": {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "bytes": 0},
                "monthly": {"month": datetime.now(timezone.utc).strftime("%Y-%m"), "bytes": 0}
            }
        }

        await self.dbs["tracking"]["api_tokens"].insert_one(token_doc)
        return convert_objectid_to_str(token_doc)

    #----- Return the user's token, creating one if none exists
    async def ensure_api_token_for_user(self, user_id: int, name: str = None) -> Optional[dict]:
        if not user_id:
            return None
        existing = await self.dbs["tracking"]["api_tokens"].find_one({"user_id": user_id})
        if existing:
            return convert_objectid_to_str(existing)
        return await self.add_api_token(name or f"User {user_id}", user_id=user_id)

    async def align_token_with_subscription(self, user_id: int) -> None:
        if not SettingsManager.current().subscription:
            return
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"user_id": user_id})
        if doc:
            await self.dbs["tracking"]["api_tokens"].update_one(
                {"_id": doc["_id"]},
                {"$set": {"subscription_exempt": False, "expires_at": None}},
            )

    #----- Toggle a token's lifetime (subscription-exempt) flag
    async def set_token_lifetime(self, token: str, exempt: bool) -> bool:
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token}, {"$set": {"subscription_exempt": bool(exempt)}}
        )
        return result.modified_count > 0

    #----- Set/extend/reduce a token's own expiry (used when subscription mode is off).
    #----- 'set' with 0/None days clears the expiry (never expires).
    async def update_token_expiry(self, token: str, action: str = "set", days: int = 0) -> Optional[dict]:
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        if not doc:
            return None
        now = datetime.utcnow()
        current = doc.get("expires_at")
        if action == "set":
            new_expiry = now + timedelta(days=days) if days and days > 0 else None
        elif action == "extend":
            base = current if (current and current > now) else now
            new_expiry = base + timedelta(days=days)
        elif action == "reduce":
            base = current if current else now
            new_expiry = base - timedelta(days=days)
            if new_expiry < now:
                new_expiry = now
        else:
            return None
        await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {"expires_at": new_expiry, "subscription_exempt": new_expiry is None}},
        )
        return await self.get_api_token(token)

    #----- Mark every token that isn't linked to a user as lifetime
    async def grant_lifetime_to_unlinked(self) -> int:
        result = await self.dbs["tracking"]["api_tokens"].update_many(
            {"$or": [{"user_id": None}, {"user_id": {"$exists": False}}]},
            {"$set": {"subscription_exempt": True}},
        )
        return result.modified_count

    #----- Count tokens that would stop working if subscription mode is enabled
    async def count_uncovered_tokens(self) -> int:
        tokens = await self.get_all_api_tokens()
        now = datetime.utcnow()
        count = 0
        for t in tokens:
            if t.get("is_admin") or t.get("subscription_exempt"):
                continue
            exp = t.get("expires_at")
            if exp and exp > now:
                continue  #----- token has its own live expiry (honoured in sub mode)
            uid = t.get("user_id")
            if not uid:
                count += 1
                continue
            if not self.is_subscription_active(await self.get_user(int(uid))):
                count += 1
        return count

    async def get_api_token(self, token: str) -> Optional[dict]:
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        return convert_objectid_to_str(doc) if doc else None

    #----- The (single) token linked to a given user_id, if any
    async def get_api_token_by_user(self, user_id: int) -> Optional[dict]:
        doc = await self.dbs["tracking"]["api_tokens"].find_one({"user_id": user_id})
        return convert_objectid_to_str(doc) if doc else None

    async def get_all_api_tokens(self) -> List[dict]:
        cursor = self.dbs["tracking"]["api_tokens"].find().sort("created_at", DESCENDING)
        tokens = await cursor.to_list(None)
        return [convert_objectid_to_str(token) for token in tokens]

    async def revoke_api_token(self, token: str) -> bool:
        result = await self.dbs["tracking"]["api_tokens"].delete_one({"token": token})
        return result.deleted_count > 0

    async def link_token_user(self, token: str, user_id: int, name: str = None) -> bool:
        #----- Link an existing token to a Telegram user_id; elevate to admin when
        #----- the linked user is the configured owner. Optionally overwrite the name.
        update = {"user_id": user_id, "is_admin": self._is_owner(user_id)}
        if name:
            update["name"] = name
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token}, {"$set": update}
        )
        return result.modified_count > 0

    @staticmethod
    def _is_owner(user_id) -> bool:
        try:
            return user_id is not None and int(user_id) == int(Telegram.OWNER_ID)
        except (TypeError, ValueError):
            return False

    async def update_token_usage(self, token: str, bytes_delta: int):
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month_str = datetime.now(timezone.utc).strftime("%Y-%m")
        
        token_doc = await self.dbs["tracking"]["api_tokens"].find_one({"token": token})
        if not token_doc:
             return

        current_daily = token_doc.get("usage", {}).get("daily", {})
        if current_daily.get("date") != today_str:
            await self.dbs["tracking"]["api_tokens"].update_one(
                {"token": token},
                {"$set": {"usage.daily": {"date": today_str, "bytes": 0}}}
            )

        current_monthly = token_doc.get("usage", {}).get("monthly", {})
        if current_monthly.get("month") != month_str:
            await self.dbs["tracking"]["api_tokens"].update_one(
                {"token": token},
                {"$set": {"usage.monthly": {"month": month_str, "bytes": 0}}}
            )

        await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {
                "$inc": {
                    "usage.total_bytes": bytes_delta,
                    "usage.daily.bytes": bytes_delta,
                    "usage.monthly.bytes": bytes_delta
                }
            }
        )

    async def update_api_token_limits(self, token: str, daily_limit_gb: float, monthly_limit_gb: float) -> bool:
        result = await self.dbs["tracking"]["api_tokens"].update_one(
            {"token": token},
            {"$set": {
                "limits": {
                    "daily_limit_gb": daily_limit_gb if daily_limit_gb else 0,
                    "monthly_limit_gb": monthly_limit_gb if monthly_limit_gb else 0
                }
            }}
        )
        return result.modified_count > 0

    #-----
    #----- Admin / Link Checker Methods
    #-----
    async def flag_dead_link(self, media_type: str, tmdb_id: int, db_index: int, quality_id: str) -> bool:
        #----- Flag a specific telegram quality entry as is_dead=True
        db_key = f"storage_{db_index}"
        
        if media_type == "movie":
            #----- Direct update in the telegram array for movies
            result = await self.dbs[db_key]["movie"].update_one(
                {"tmdb_id": tmdb_id, "telegram.id": quality_id},
                {"$set": {"telegram.$.is_dead": True, "updated_on": datetime.utcnow()}}
            )
            return result.modified_count > 0
            
        elif media_type == "tv":
            #----- Nested update for TV (arrayFilters needed since we don't know the exact indices)
            #----- Find the TV show docs
            tv = await self.dbs[db_key]["tv"].find_one({"tmdb_id": tmdb_id})
            if not tv or "seasons" not in tv:
                return False
                
            found = False
            for s_idx, season in enumerate(tv["seasons"]):
                for e_idx, episode in enumerate(season.get("episodes", [])):
                    for q_idx, quality in enumerate(episode.get("telegram", [])):
                        if quality.get("id") == quality_id:
                            tv["seasons"][s_idx]["episodes"][e_idx]["telegram"][q_idx]["is_dead"] = True
                            found = True
                            break
                    if found: break
                if found: break
                
            if found:
                tv["updated_on"] = datetime.utcnow()
                result = await self.dbs[db_key]["tv"].replace_one({"tmdb_id": tmdb_id}, tv)
                return result.modified_count > 0
                
        return False

    async def get_all_dead_links(self) -> List[dict]:
        #----- Flattened list of all dead links across storage DBs for the Admin UI
        dead_links = []
        
        for i in range(1, self.current_db_index + 1):
            db_key = f"storage_{i}"
            db = self.dbs[db_key]
            
            #----- Scan Movies ---
            #----- Match any movie where at least one telegram entry has is_dead=True
            movie_cursor = db["movie"].find({"telegram.is_dead": True})
            async for movie in movie_cursor:
                for quality in movie.get("telegram", []):
                    if quality.get("is_dead"):
                        dead_links.append({
                            "type": "movie",
                            "tmdb_id": movie.get("tmdb_id"),
                            "db_index": movie.get("db_index", i),
                            "title": movie.get("title"),
                            "year": movie.get("year"),
                            "poster": movie.get("poster"),
                            "quality_id": quality.get("id"),
                            "quality": quality.get("quality"),
                            "size": quality.get("size"),
                            "date_added": quality.get("date_added")
                        })
                        
            #----- Scan TV Shows ---
            #----- Match any TV where seasons.episodes.telegram.is_dead=True
            tv_cursor = db["tv"].find({"seasons.episodes.telegram.is_dead": True})
            async for tv in tv_cursor:
                title = tv.get("title")
                year = tv.get("year")
                poster = tv.get("poster")
                for season in tv.get("seasons", []):
                    s_num = season.get("season_number")
                    for ep in season.get("episodes", []):
                        e_num = ep.get("episode_number")
                        for quality in ep.get("telegram", []):
                            if quality.get("is_dead"):
                                dead_links.append({
                                    "type": "tv",
                                    "tmdb_id": tv.get("tmdb_id"),
                                    "db_index": tv.get("db_index", i),
                                    "title": f"{title} (S{s_num:02d}E{e_num:02d})",
                                    "year": year,
                                    "poster": poster,
                                    "season": s_num,
                                    "episode": e_num,
                                    "quality_id": quality.get("id"),
                                    "quality": quality.get("quality"),
                                    "size": quality.get("size"),
                                    "date_added": quality.get("date_added")
                                })
                                
        return dead_links

    #-----
    #----- Stream Analytics
    #-----

    async def log_stream_stats(self, stats: dict) -> None:
        #----- Persist a finished-stream record to the tracking DB for analytics
        try:
            record = {
                "stream_id":   stats.get("stream_id"),
                "msg_id":      stats.get("msg_id"),
                "chat_id":     stats.get("chat_id"),
                "dc_id":       stats.get("dc_id"),
                "title":       stats.get("meta", {}).get("title"),  #----- Added title
                "user_name":   stats.get("meta", {}).get("user_name"),
                "token":       stats.get("meta", {}).get("token"),
                "client_index": stats.get("client_index"),
                "total_bytes": stats.get("total_bytes", 0),
                "duration_sec": round(stats.get("duration", 0.0), 2),
                "avg_mbps":    round(stats.get("avg_mbps", 0.0), 3),
                "peak_mbps":   round(stats.get("peak_mbps", 0.0), 3),
                "status":      stats.get("status", "finished"),
                "parallelism": stats.get("parallelism"),
                "chunk_size":  stats.get("chunk_size"),
                "logged_at":   datetime.utcnow(),
            }
            await self.dbs["tracking"]["stream_analytics"].insert_one(record)
        except Exception as e:
            LOGGER.warning(f"Stream analytics log failed: {e}")

    async def get_stream_analytics(self, limit: int = 200) -> dict:
        #----- Return summary stats + recent stream records from the tracking DB
        try:
            col = self.dbs["tracking"]["stream_analytics"]

            #----- Aggregate totals
            pipeline = [
                {"$group": {
                    "_id": None,
                    "total_streams":     {"$sum": 1},
                    "total_bytes":       {"$sum": "$total_bytes"},
                    "avg_speed":         {"$avg": "$avg_mbps"},
                    "peak_speed":        {"$max": "$peak_mbps"},
                    "avg_duration":      {"$avg": "$duration_sec"},
                }},
            ]
            agg = await col.aggregate(pipeline).to_list(1)
            summary = agg[0] if agg else {}
            summary.pop("_id", None)

            #----- Per-client breakdown
            per_client_pipeline = [
                {"$group": {
                    "_id":          "$client_index",
                    "streams":      {"$sum": 1},
                    "avg_mbps":     {"$avg": "$avg_mbps"},
                    "peak_mbps":    {"$max": "$peak_mbps"},
                    "total_bytes":  {"$sum": "$total_bytes"},
                }},
                {"$sort": {"_id": 1}},
            ]
            per_client = await col.aggregate(per_client_pipeline).to_list(None)
            for row in per_client:
                row["client_index"] = row.pop("_id")
                row["avg_mbps"]     = round(row.get("avg_mbps", 0), 3)
                row["peak_mbps"]    = round(row.get("peak_mbps", 0), 3)

            #----- Recent records (newest first)
            recent_cursor = col.find(
                {},
                {"_id": 0, "stream_id": 1, "client_index": 1, "dc_id": 1,
                 "total_bytes": 1, "duration_sec": 1, "avg_mbps": 1,
                 "peak_mbps": 1, "status": 1, "logged_at": 1, "title": 1}
            ).sort("logged_at", DESCENDING).limit(limit)
            recent = await recent_cursor.to_list(None)
            for r in recent:
                if "logged_at" in r:
                    r["logged_at"] = r["logged_at"].isoformat()

            #----- Most-streamed titles
            top_titles = await col.aggregate([
                {"$match": {"title": {"$nin": [None, ""]}}},
                {"$group": {"_id": "$title", "streams": {"$sum": 1}, "total_bytes": {"$sum": "$total_bytes"}}},
                {"$sort": {"streams": -1}},
                {"$limit": 8},
            ]).to_list(None)
            for r in top_titles:
                r["title"] = r.pop("_id")

            #----- Heaviest viewers (by data transferred)
            top_users = await col.aggregate([
                {"$match": {"user_name": {"$nin": [None, ""]}}},
                {"$group": {"_id": "$user_name", "streams": {"$sum": 1}, "total_bytes": {"$sum": "$total_bytes"}}},
                {"$sort": {"total_bytes": -1}},
                {"$limit": 8},
            ]).to_list(None)
            for r in top_users:
                r["user"] = r.pop("_id")

            #----- Streams & data per day (last 14 days, chronological)
            per_day = await col.aggregate([
                {"$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$logged_at"}},
                    "streams": {"$sum": 1},
                    "total_bytes": {"$sum": "$total_bytes"},
                }},
                {"$sort": {"_id": -1}},
                {"$limit": 14},
            ]).to_list(None)
            for r in per_day:
                r["date"] = r.pop("_id")
            per_day.reverse()

            distinct_users = await col.distinct("user_name")
            summary["active_users"] = len([u for u in distinct_users if u and u != "Unknown"])

            return {
                "summary":    summary,
                "per_client": per_client,
                "top_titles": top_titles,
                "top_users":  top_users,
                "per_day":    per_day,
                "recent":     recent,
            }
        except Exception as e:
            LOGGER.error(f"get_stream_analytics error: {e}")
            return {"summary": {}, "per_client": [], "top_titles": [], "top_users": [], "per_day": [], "recent": []}



    @staticmethod
    def _merge_telegram_lists(primary: List[dict], secondary: List[dict]) -> List[dict]:
        merged = list(primary or [])
        existing_ids = {q.get("id") for q in merged if q.get("id")}
        existing_groups = {q.get("group_key") for q in merged if q.get("group_key")}
        for q in (secondary or []):
            group_key = q.get("group_key")
            if group_key and group_key in existing_groups:
                continue
            if q.get("id") and q.get("id") in existing_ids:
                continue
            merged.append(q)
            if q.get("id"):
                existing_ids.add(q.get("id"))
            if group_key:
                existing_groups.add(group_key)
        return merged

    def _merge_season_lists(self, primary: List[dict], secondary: List[dict]) -> List[dict]:
        merged = list(primary or [])
        season_map = {s.get("season_number"): s for s in merged}
        for season in (secondary or []):
            season_number = season.get("season_number")
            target_season = season_map.get(season_number)
            if not target_season:
                merged.append(season)
                season_map[season_number] = season
                continue

            target_season.setdefault("episodes", [])
            episode_map = {e.get("episode_number"): e for e in target_season["episodes"]}
            for episode in season.get("episodes", []):
                episode_number = episode.get("episode_number")
                target_episode = episode_map.get(episode_number)
                if not target_episode:
                    target_season["episodes"].append(episode)
                    episode_map[episode_number] = episode
                    continue
                target_episode["telegram"] = self._merge_telegram_lists(
                    target_episode.get("telegram", []), episode.get("telegram", [])
                )
        return merged

    async def replace_media_metadata(
        self,
        media_type: str,
        tmdb_id: int,
        db_index: int,
        metadata: Dict[str, Any]
    ) -> Optional[dict]:
        db_key = f"storage_{db_index}"
        collection_name = self._collection_for(media_type)
        collection = self.dbs[db_key][collection_name]

        current_doc = await collection.find_one({"tmdb_id": int(tmdb_id)})
        if not current_doc:
            return None

        source_id = current_doc["_id"]

        def _pick(key: str):
            new_value = metadata.get(key)
            if new_value in (None, "", [], {}, 0, 0.0, "0"):
                return current_doc.get(key)
            return new_value

        new_tmdb_id = int(metadata.get("tmdb_id") or tmdb_id)
        new_imdb_id = _pick("imdb_id")

        if collection_name == "movie":
            current_doc.update({
                "tmdb_id": new_tmdb_id,
                "imdb_id": new_imdb_id,
                "title": _pick("title"),
                "release_year": _pick("release_year"),
                "rating": _pick("rating"),
                "description": _pick("description"),
                "poster": _pick("poster"),
                "backdrop": _pick("backdrop"),
                "logo": _pick("logo"),
                "genres": _pick("genres"),
                "cast": _pick("cast"),
                "runtime": _pick("runtime"),
                "media_type": "movie",
                "telegram": current_doc.get("telegram", []),
                "updated_on": datetime.utcnow(),
            })
        else:
            current_doc.update({
                "tmdb_id": new_tmdb_id,
                "imdb_id": new_imdb_id,
                "title": _pick("title"),
                "release_year": _pick("release_year"),
                "rating": _pick("rating"),
                "description": _pick("description"),
                "poster": _pick("poster"),
                "backdrop": _pick("backdrop"),
                "logo": _pick("logo"),
                "genres": _pick("genres"),
                "cast": _pick("cast"),
                "runtime": _pick("runtime"),
                "media_type": "tv",
                "seasons": current_doc.get("seasons", []),
                "updated_on": datetime.utcnow(),
            })

        identity_filters = []
        if new_imdb_id:
            identity_filters.append({"imdb_id": new_imdb_id})
        identity_filters.append({"tmdb_id": new_tmdb_id})

        existing_other = await collection.find_one({
            "$and": [{"$or": identity_filters}, {"_id": {"$ne": source_id}}]
        })

        if existing_other:
            if collection_name == "movie":
                existing_other["telegram"] = self._merge_telegram_lists(
                    existing_other.get("telegram", []), current_doc.get("telegram", [])
                )
            else:
                existing_other["seasons"] = self._merge_season_lists(
                    existing_other.get("seasons", []), current_doc.get("seasons", [])
                )

            for field in (
                "tmdb_id", "imdb_id", "title", "release_year", "rating",
                "description", "poster", "backdrop", "logo", "genres",
                "cast", "runtime", "media_type",
            ):
                if field in current_doc:
                    existing_other[field] = current_doc[field]
            existing_other["updated_on"] = datetime.utcnow()

            await collection.delete_one({"_id": source_id})
            await collection.replace_one({"_id": existing_other["_id"]}, existing_other)

            updated_doc = await collection.find_one({"_id": existing_other["_id"]})
            return convert_objectid_to_str(updated_doc) if updated_doc else None
        await collection.replace_one({"_id": source_id}, current_doc)

        updated_doc = await collection.find_one({"_id": source_id})
        return convert_objectid_to_str(updated_doc) if updated_doc else None
