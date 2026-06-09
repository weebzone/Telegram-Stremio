import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "DATABASE",
    "mongodb://localhost:27017/iptv_tracking,mongodb://localhost:27017/iptv_storage",
)

from Backend.fastapi.routes import iptv_routes, stremio_routes
from Backend.helper.iptv import (
    IPTV_CATALOG_ID,
    build_iptv_streams,
    channel_is_eligible,
    get_iptv_settings,
    iptv_meta,
    sign_proxy_target,
    sync_iptv_data,
    update_iptv_settings,
    verify_proxy_target,
)


class IptvHelperTests(unittest.TestCase):
    def test_channel_filter_rejects_wrong_country_blocked_and_unsafe_channels(self):
        base = {
            "id": "Test.in",
            "country": "IN",
            "is_nsfw": False,
            "closed": None,
            "replaced_by": None,
        }
        self.assertTrue(channel_is_eligible(base, {"IN"}, set()))
        self.assertFalse(channel_is_eligible({**base, "country": "US"}, {"IN"}, set()))
        self.assertFalse(channel_is_eligible({**base, "is_nsfw": True}, {"IN"}, set()))
        self.assertFalse(channel_is_eligible({**base, "closed": "2025-01-01"}, {"IN"}, set()))
        self.assertFalse(channel_is_eligible({**base, "replaced_by": "Other.in"}, {"IN"}, set()))
        self.assertFalse(channel_is_eligible(base, {"IN"}, {"Test.in"}))

    def test_direct_and_header_streams_are_returned_in_direct_first_order(self):
        channel = {
            "_id": "News.in",
            "name": "News",
            "streams": [
                {
                    "id": "direct",
                    "url": "https://example.test/live.m3u8",
                    "quality": "1080p",
                    "request_headers": {},
                },
                {
                    "id": "headers",
                    "url": "https://example.test/secure.m3u8",
                    "quality": "720p",
                    "request_headers": {
                        "Referer": "https://example.test/",
                        "User-Agent": "Test Agent",
                    },
                },
            ],
        }
        with (
            patch.object(stremio_routes.Telegram, "BASE_URL", "https://addon.test"),
            patch.object(stremio_routes.Telegram, "IPTV_PROXY_FALLBACK_ENABLED", True),
            patch("Backend.helper.iptv.Telegram.BASE_URL", "https://addon.test"),
            patch("Backend.helper.iptv.Telegram.IPTV_PROXY_FALLBACK_ENABLED", True),
        ):
            streams = build_iptv_streams(channel, "token123")

        self.assertEqual(len(streams), 3)
        self.assertEqual(streams[0]["url"], "https://example.test/live.m3u8")
        self.assertEqual(streams[1]["url"], "https://example.test/secure.m3u8")
        self.assertEqual(
            streams[1]["behaviorHints"]["proxyHeaders"]["request"]["Referer"],
            "https://example.test/",
        )
        self.assertEqual(
            streams[2]["url"],
            "https://addon.test/iptv/token123/stream/headers",
        )
        self.assertIn("Uses VPS bandwidth", streams[2]["title"])

    def test_signed_proxy_target_round_trip_and_tamper_rejection(self):
        with patch("Backend.helper.iptv.Telegram.IPTV_PROXY_SECRET", "test-secret"):
            signed = sign_proxy_target("stream1", "https://example.test/segment.ts")
            payload = verify_proxy_target(signed)
            self.assertEqual(payload["s"], "stream1")
            self.assertEqual(payload["u"], "https://example.test/segment.ts")
            with self.assertRaises(ValueError):
                verify_proxy_target(signed[:-1] + ("0" if signed[-1] != "0" else "1"))

    def test_hls_rewriter_proxies_relative_segments_and_key_urls(self):
        manifest = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:5,\n"
            "segments/one.ts\n"
        )
        with (
            patch.object(iptv_routes.Telegram, "BASE_URL", "https://addon.test"),
            patch("Backend.helper.iptv.Telegram.IPTV_PROXY_SECRET", "test-secret"),
        ):
            result = iptv_routes._rewrite_hls_manifest(
                manifest,
                "https://origin.test/live/master.m3u8",
                "token123",
                "stream1",
            )

        self.assertIn("https://addon.test/iptv/token123/fetch/", result)
        self.assertNotIn('URI="key.bin"', result)
        self.assertNotIn("\nsegments/one.ts\n", result)

    def test_meta_uses_tv_type_and_stable_iptv_id(self):
        meta = iptv_meta(
            {
                "_id": "News.in",
                "name": "News",
                "logo": "https://example.test/logo.png",
                "categories": ["News"],
                "languages": ["Hindi"],
                "country_name": "India",
            }
        )
        self.assertEqual(meta["id"], "iptv:News.in")
        self.assertEqual(meta["type"], "tv")
        self.assertEqual(meta["genres"], ["News"])


class _AsyncCursor:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        self.index = 0
        return self

    async def __anext__(self):
        if self.index >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.index]
        self.index += 1
        return item


class _StateCollection:
    def __init__(self, initial=None):
        self.docs = dict(initial or {})
        self.last_update = None

    async def find_one(self, query):
        return self.docs.get(query.get("_id"))

    async def update_one(self, query, update, upsert=False):
        self.last_update = update
        doc = dict(self.docs.get(query.get("_id")) or {"_id": query.get("_id")})
        doc.update(update.get("$set") or {})
        for key in update.get("$unset") or {}:
            doc.pop(key, None)
        self.docs[query.get("_id")] = doc


class _IptvCollection:
    def __init__(self):
        self.docs = {}
        self.bulk_count = 0

    def find(self, query=None, projection=None):
        hidden_docs = [doc for doc in self.docs.values() if doc.get("hidden")]
        return _AsyncCursor(hidden_docs)

    async def bulk_write(self, operations, ordered=False):
        self.bulk_count = len(operations)
        for operation in operations:
            self.docs[operation._filter["_id"]] = operation._doc

    async def delete_many(self, query):
        sync_id = (query.get("sync_id") or {}).get("$ne")
        if sync_id:
            self.docs = {
                key: value
                for key, value in self.docs.items()
                if value.get("sync_id") == sync_id
            }

    async def create_index(self, *args, **kwargs):
        return None

    async def count_documents(self, query):
        if query.get("hidden", {}).get("$ne") is True:
            return len([doc for doc in self.docs.values() if not doc.get("hidden")])
        return len(self.docs)


class _IptvDb:
    def __init__(self, state_initial=None):
        self.state = _StateCollection(state_initial)
        self.iptv_channels = _IptvCollection()
        self.dbs = {
            "tracking": {
                "state": self.state,
                "iptv_channels": self.iptv_channels,
            }
        }


class _StreamsResponse:
    status_code = 200
    headers = {"etag": "test-etag", "last-modified": "today"}

    def __init__(self, streams):
        self.streams = streams

    def json(self):
        return self.streams

    def raise_for_status(self):
        return None


class _HttpClient:
    def __init__(self, streams):
        self.streams = streams

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        return _StreamsResponse(self.streams)


class IptvUnlimitedTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_ignore_and_unset_legacy_channel_limit(self):
        db = _IptvDb({"iptv_settings": {"_id": "iptv_settings", "enabled": True, "channel_limit": 100}})

        settings = await get_iptv_settings(db)
        self.assertEqual(settings["channel_limit"], 0)
        self.assertTrue(settings["unlimited"])

        updated = await update_iptv_settings(db, {"enabled": True, "channel_limit": 100})
        self.assertEqual(updated["channel_limit"], 0)
        self.assertIn("channel_limit", db.state.last_update.get("$unset", {}))
        self.assertNotIn("channel_limit", db.state.docs["iptv_settings"])

    async def test_sync_keeps_all_playable_indian_channels_above_old_pilot_cap(self):
        channel_count = 125
        channels = [
            {
                "id": f"Channel{i}.in",
                "name": f"Channel {i}",
                "country": "IN",
                "categories": ["news"],
                "is_nsfw": False,
                "closed": None,
                "replaced_by": None,
            }
            for i in range(channel_count)
        ]
        streams = [
            {
                "channel": f"Channel{i}.in",
                "feed": None,
                "title": "",
                "url": f"https://example.test/{i}.m3u8",
                "quality": "720p",
            }
            for i in range(channel_count)
        ]

        async def fake_fetch_json(client, dataset):
            return {
                "channels": channels,
                "blocklist": [],
                "feeds": [],
                "logos": [],
                "categories": [{"id": "news", "name": "News"}],
                "languages": [],
                "countries": [{"code": "IN", "name": "India", "flag": "IN"}],
            }[dataset]

        db = _IptvDb({"iptv_settings": {"_id": "iptv_settings", "enabled": True, "channel_limit": 100}})
        with (
            patch("Backend.helper.iptv._fetch_json", fake_fetch_json),
            patch("Backend.helper.iptv.httpx.AsyncClient", lambda *args, **kwargs: _HttpClient(streams)),
        ):
            result = await sync_iptv_data(db, force=True)

        self.assertEqual(result["counts"]["selected_channels"], channel_count)
        self.assertEqual(result["counts"]["source_country_channels"], channel_count)
        self.assertEqual(result["counts"]["playable_country_channels"], channel_count)
        self.assertEqual(db.iptv_channels.bulk_count, channel_count)
        self.assertNotIn("channel_limit", db.state.docs["iptv_settings"])


class _DistinctCollection:
    async def distinct(self, field, query):
        return ["News", "Entertainment"]


class _TrackingDB:
    def __getitem__(self, name):
        return _DistinctCollection()


class _ManifestDB:
    dbs = {"tracking": _TrackingDB()}

    async def get_custom_catalogs(self, visible_only=False):
        return []


class IptvStremioRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_advertises_tv_catalog_and_prefix(self):
        with (
            patch.object(stremio_routes, "db", _ManifestDB()),
            patch.object(stremio_routes.Telegram, "HIDE_CATALOG", False),
            patch.object(
                stremio_routes,
                "get_iptv_settings",
                AsyncMock(return_value={"enabled": True}),
            ),
        ):
            manifest = await stremio_routes.get_manifest(
                "token123",
                token_data={},
            )

        self.assertIn("tv", manifest["types"])
        self.assertIn("iptv:", manifest["idPrefixes"])
        live_catalog = [
            item
            for item in manifest["catalogs"]
            if item["id"] == IPTV_CATALOG_ID
        ]
        self.assertEqual(len(live_catalog), 1)
        self.assertEqual(live_catalog[0]["type"], "tv")

    async def test_tv_catalog_returns_iptv_metas(self):
        channel = {
            "_id": "News.in",
            "stremio_id": "iptv:News.in",
            "name": "News",
            "categories": ["News"],
            "languages": ["Hindi"],
        }
        with (
            patch.object(stremio_routes.Telegram, "HIDE_CATALOG", False),
            patch.object(
                stremio_routes,
                "get_iptv_settings",
                AsyncMock(return_value={"enabled": True}),
            ),
            patch.object(
                stremio_routes,
                "list_iptv_channels",
                AsyncMock(return_value={"channels": [channel]}),
            ),
        ):
            result = await stremio_routes.get_catalog(
                "token123",
                "tv",
                IPTV_CATALOG_ID,
                token_data={},
            )

        self.assertEqual(result["metas"][0]["id"], "iptv:News.in")
        self.assertEqual(result["metas"][0]["type"], "tv")

    async def test_tv_stream_route_returns_direct_url(self):
        channel = {
            "_id": "News.in",
            "name": "News",
            "streams": [
                {
                    "id": "direct",
                    "url": "https://example.test/live.m3u8",
                    "quality": "720p",
                    "request_headers": {},
                }
            ],
        }
        with (
            patch.object(
                stremio_routes,
                "get_iptv_settings",
                AsyncMock(return_value={"enabled": True}),
            ),
            patch.object(
                stremio_routes,
                "get_iptv_channel",
                AsyncMock(return_value=channel),
            ),
        ):
            result = await stremio_routes.get_streams(
                "token123",
                "tv",
                "iptv:News.in",
                token_data={},
            )

        self.assertEqual(result["streams"][0]["url"], "https://example.test/live.m3u8")
        self.assertTrue(result["streams"][0]["behaviorHints"]["notWebReady"])


if __name__ == "__main__":
    unittest.main()
