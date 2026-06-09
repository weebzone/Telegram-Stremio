import os

os.environ.setdefault(
    "DATABASE",
    "mongodb://localhost:27017/unmatched_tracking,mongodb://localhost:27017/unmatched_storage",
)

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from Backend.fastapi.routes import api_routes


class UnmatchedMediaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake_db = SimpleNamespace(
            list_unmatched_media=AsyncMock(),
            get_unmatched_media=AsyncMock(),
            update_unmatched_suggestions=AsyncMock(return_value=True),
            mark_unmatched_status=AsyncMock(return_value=True),
            insert_media=AsyncMock(return_value="media_123"),
        )
        self.db_patch = patch.object(api_routes, "db", self.fake_db)
        self.db_patch.start()

    async def asyncTearDown(self):
        self.db_patch.stop()

    async def test_list_unmatched_media_api_returns_items(self):
        self.fake_db.list_unmatched_media.return_value = [{"_id": "1", "title": "Example"}]

        result = await api_routes.list_unmatched_media_api()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"][0]["title"], "Example")
        self.fake_db.list_unmatched_media.assert_awaited_once_with(status="open")

    async def test_search_unmatched_media_api_stores_suggestions(self):
        self.fake_db.get_unmatched_media.return_value = {
            "_id": "1",
            "title": "Example Movie",
            "file_name": "Example.Movie.2024.mkv",
        }

        with patch.object(api_routes, "search_movie_candidates", AsyncMock(return_value=[{"title": "Example Movie"}])):
            result = await api_routes.search_unmatched_media_api(
                "1",
                {"query": "Example Movie", "media_type": "movie", "year": 2024},
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"][0]["title"], "Example Movie")
        self.fake_db.update_unmatched_suggestions.assert_awaited_once()

    async def test_apply_unmatched_media_api_indexes_selected_title(self):
        self.fake_db.get_unmatched_media.return_value = {
            "_id": "1",
            "title": "Example Movie 2024",
            "file_name": "Example.Movie.2024.mkv",
            "origin_chat_id": -1001234567890,
            "origin_msg_id": 42,
        }

        with (
            patch.object(api_routes, "clean_filename", lambda value: value),
            patch.object(api_routes, "remove_urls", lambda value: value),
            patch.object(api_routes, "metadata", AsyncMock(return_value={"tmdb_id": 999})),
        ):
            result = await api_routes.apply_unmatched_media_api("1", {"selected_id": "tt9999999"})

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["updated_id"], "media_123")
        self.fake_db.insert_media.assert_awaited_once()
        self.fake_db.mark_unmatched_status.assert_awaited_once_with("1", "resolved", {"resolved_by": "manual_apply"})

    async def test_reprocess_unmatched_media_api_marks_resolved(self):
        self.fake_db.get_unmatched_media.return_value = {
            "_id": "1",
            "title": "Example Movie 2024",
            "file_name": "Example.Movie.2024.mkv",
            "origin_chat_id": -1001234567890,
            "origin_msg_id": 42,
        }

        with (
            patch.object(api_routes, "clean_filename", lambda value: value),
            patch.object(api_routes, "remove_urls", lambda value: value),
            patch.object(api_routes, "metadata", AsyncMock(return_value={"tmdb_id": 999})),
        ):
            result = await api_routes.reprocess_unmatched_media_api("1")

        self.assertEqual(result["status"], "success")
        self.fake_db.mark_unmatched_status.assert_awaited_once_with("1", "resolved", {"resolved_by": "reprocess"})

    async def test_dismiss_unmatched_media_api(self):
        result = await api_routes.dismiss_unmatched_media_api("1")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["message"], "Unmatched item dismissed")
        self.fake_db.mark_unmatched_status.assert_awaited_once_with("1", "dismissed")


if __name__ == "__main__":
    unittest.main()
