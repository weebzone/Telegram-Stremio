import os

os.environ.setdefault(
    "DATABASE",
    "mongodb://localhost:27017/quality_tracking,mongodb://localhost:27017/quality_storage",
)

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from Backend.fastapi.routes import api_routes, stremio_routes
from Backend.helper.database import Database


class QualityControlTests(unittest.IsolatedAsyncioTestCase):
    def test_apply_quality_flags_and_clear(self):
        quality = {"id": "q1", "quality": "1080p"}

        Database._apply_quality_flags(
            quality,
            {
                "recommended": True,
                "hidden_from_stremio": True,
                "quality_note": "bad seek",
                "flagged_duplicate": True,
            },
        )

        self.assertTrue(quality["recommended"])
        self.assertTrue(quality["hidden_from_stremio"])
        self.assertEqual(quality["quality_note"], "bad seek")
        self.assertTrue(quality["flagged_duplicate"])

        Database._apply_quality_flags(quality, {}, clear=True)
        self.assertFalse(quality["recommended"])
        self.assertFalse(quality["hidden_from_stremio"])
        self.assertIsNone(quality["quality_note"])
        self.assertFalse(quality["flagged_duplicate"])

    def test_duplicate_group_lists_multiple_qualities(self):
        database = Database()
        doc = {
            "tmdb_id": 123,
            "db_index": 1,
            "title": "Example",
        }
        qualities = [
            {"id": "same", "quality": "1080p", "name": "one.mkv", "size": "1 GB"},
            {"id": "same", "quality": "1080p", "name": "two.mkv", "size": "1 GB"},
        ]

        group = database._duplicate_group(doc, "movie", qualities)

        self.assertEqual(group["quality_count"], 2)
        self.assertEqual(group["exact_duplicate_count"], 2)
        self.assertEqual(group["qualities"][0]["id"], "same")

    async def test_quality_flags_api_forwards_admin_selection(self):
        fake_db = SimpleNamespace(update_quality_flags=AsyncMock(return_value=True))
        payload = {
            "media_type": "movie",
            "tmdb_id": 123,
            "db_index": 1,
            "id": "q1",
            "flags": {"recommended": True},
        }

        with patch.object(api_routes, "db", fake_db):
            result = await api_routes.update_quality_flags_api(payload)

        self.assertEqual(result["status"], "success")
        fake_db.update_quality_flags.assert_awaited_once_with(
            media_type="movie",
            tmdb_id=123,
            db_index=1,
            quality_id="q1",
            flags={"recommended": True},
            season=None,
            episode=None,
            clear=False,
        )

    async def test_stremio_hides_flagged_quality_and_prioritizes_recommended(self):
        fake_db = SimpleNamespace(
            get_media_details=AsyncMock(
                return_value={
                    "telegram": [
                        {
                            "id": "hidden",
                            "name": "Hidden.Movie.2160p.mkv",
                            "quality": "2160p",
                            "size": "8 GB",
                            "hidden_from_stremio": True,
                        },
                        {
                            "id": "normal",
                            "name": "Normal.Movie.1080p.mkv",
                            "quality": "1080p",
                            "size": "4 GB",
                        },
                        {
                            "id": "recommended",
                            "name": "Recommended.Movie.720p.mkv",
                            "quality": "720p",
                            "size": "2 GB",
                            "recommended": True,
                            "quality_note": "stable",
                        },
                    ]
                }
            )
        )

        with (
            patch.object(stremio_routes, "db", fake_db),
            patch.object(stremio_routes.Telegram, "PROXY", False),
            patch.object(stremio_routes.Telegram, "SHOW_PROXY_AND_NON_PROXY_BOTH", False),
        ):
            result = await stremio_routes.get_streams(
                token="token",
                media_type="movie",
                id="tt123",
                token_data={},
            )

        urls = [stream["url"] for stream in result["streams"]]
        self.assertEqual(len(urls), 2)
        self.assertIn("recommended", urls[0])
        self.assertNotIn("hidden", " ".join(urls))
        self.assertIn("Recommended", result["streams"][0]["title"])
        self.assertIn("stable", result["streams"][0]["title"])


if __name__ == "__main__":
    unittest.main()
