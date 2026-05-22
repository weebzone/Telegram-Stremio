import hashlib
import os
import struct
import unittest


os.environ.setdefault("DATABASE", "mongodb://tracking,mongodb://storage")

from Backend.fastapi.routes.stremio_routes import build_torrent_stream
from Backend.helper.torrent_source import parse_magnet, parse_torrent
from Backend.helper.torrent_stats import (
    TrackerScrapeResult,
    aggregate_tracker_stats,
    decode_udp_scrape_response,
    parse_http_scrape_response,
)


def benc(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return benc(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(benc(item) for item in value) + b"e"
    if isinstance(value, dict):
        out = b"d"
        for key in sorted(value):
            out += benc(key)
            out += benc(value[key])
        return out + b"e"
    raise TypeError(value)


class TorrentStatsTests(unittest.TestCase):
    def test_magnet_parsing_preserves_hash_and_trackers(self):
        item = parse_magnet(
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&dn=Movie.Name.2026.1080p.mkv"
            "&tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce"
        )

        self.assertEqual(item.info_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(item.file_idx, None)
        self.assertEqual(item.file_name, "Movie.Name.2026.1080p.mkv")
        self.assertEqual(item.sources, ["tracker:udp://tracker.example:80/announce"])
        self.assertFalse(item.is_private)

    def test_torrent_parsing_preserves_file_idx_and_private_flag(self):
        info = {
            b"name": b"Show Season",
            b"private": 1,
            b"files": [
                {b"length": 100, b"path": [b"notes.txt"]},
                {b"length": 2048, b"path": [b"Show.S01E01.mkv"]},
                {b"length": 4096, b"path": [b"Show.S01E02.mkv"]},
            ],
        }
        torrent = benc({
            b"announce": b"udp://tracker.example:80/announce",
            b"info": info,
        })
        items = parse_torrent(torrent)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].file_idx, 1)
        self.assertEqual(items[0].file_name, "Show.S01E01.mkv")
        self.assertEqual(items[1].file_idx, 2)
        self.assertTrue(items[0].is_private)
        self.assertEqual(items[0].info_hash, hashlib.sha1(benc(info)).hexdigest())

    def test_http_scrape_response_parses_seeders_and_peers(self):
        info_hash = "0123456789abcdef0123456789abcdef01234567"
        payload = benc({
            b"files": {
                bytes.fromhex(info_hash): {
                    b"complete": 42,
                    b"incomplete": 8,
                    b"downloaded": 99,
                }
            }
        })
        result = parse_http_scrape_response(payload, info_hash)

        self.assertTrue(result.ok)
        self.assertEqual(result.seeders, 42)
        self.assertEqual(result.peers, 8)
        self.assertEqual(result.completed, 99)

    def test_udp_scrape_response_parses_seeders_and_peers(self):
        transaction_id = 12345
        payload = struct.pack(">IIIII", 2, transaction_id, 42, 99, 8)
        result = decode_udp_scrape_response(payload, transaction_id)

        self.assertTrue(result.ok)
        self.assertEqual(result.seeders, 42)
        self.assertEqual(result.peers, 8)
        self.assertEqual(result.completed, 99)

    def test_aggregation_uses_max_values_and_ignores_failures(self):
        stats = aggregate_tracker_stats(
            [
                TrackerScrapeResult("udp://a", seeders=10, peers=5, completed=20, ok=True),
                TrackerScrapeResult("udp://b", seeders=40, peers=2, completed=10, ok=True),
                TrackerScrapeResult("udp://c", error="timeout"),
            ],
            trackers_checked=3,
        )

        self.assertEqual(stats["status"], "ok")
        self.assertEqual(stats["seeders"], 40)
        self.assertEqual(stats["peers"], 5)
        self.assertEqual(stats["completed"], 20)
        self.assertEqual(stats["trackers_responded"], 2)

    def test_torrent_stream_title_adds_stats_only_when_available(self):
        quality = {
            "source_type": "torrent",
            "info_hash": "0123456789abcdef0123456789abcdef01234567",
            "file_idx": 2,
            "sources": ["tracker:udp://tracker.example:80/announce"],
            "filename": "Show.S01E02.mkv",
            "video_size": 123456,
        }

        without_stats = build_torrent_stream(quality, "Telegram 1080p", "title")
        self.assertNotIn("Seeds:", without_stats["title"])

        with_stats = build_torrent_stream(
            quality,
            "Telegram 1080p",
            "title",
            {"status": "ok", "seeders": 42, "peers": 8},
        )
        self.assertIn("Seeds: 42 | Peers: 8", with_stats["title"])
        self.assertEqual(with_stats["infoHash"], quality["info_hash"])
        self.assertEqual(with_stats["fileIdx"], 2)


if __name__ == "__main__":
    unittest.main()
