import os

os.environ.setdefault(
    "DATABASE",
    "mongodb://localhost:27017/torrent_tracking,mongodb://localhost:27017/torrent_storage",
)

import base64
import hashlib
import unittest

from Backend.helper.torrent_source import extract_magnet_links, parse_magnet, parse_torrent


def _bencode(value):
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return f"{len(value)}:".encode() + value
    if isinstance(value, str):
        data = value.encode()
        return f"{len(data)}:".encode() + data
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value.keys()):
            key_bytes = key.encode() if isinstance(key, str) else key
            parts.append(f"{len(key_bytes)}:".encode() + key_bytes)
            parts.append(_bencode(value[key]))
        return b"d" + b"".join(parts) + b"e"
    raise TypeError(f"Unsupported bencode type: {type(value)!r}")


class TorrentSourceTests(unittest.TestCase):
    def test_extract_magnet_links(self):
        text = 'here is magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Example.'
        self.assertEqual(
            extract_magnet_links(text),
            ["magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Example"],
        )

    def test_parse_magnet_hex_info_hash(self):
        magnet = "magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&dn=Example%20Movie&tr=http://tracker.example/announce"
        item = parse_magnet(magnet)

        self.assertEqual(item.info_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(item.display_name, "Example Movie")
        self.assertEqual(item.file_name, "Example Movie")
        self.assertEqual(item.sources, ["tracker:http://tracker.example/announce"])
        self.assertEqual(item.file_idx, None)

    def test_parse_magnet_base32_info_hash(self):
        raw_hash = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        encoded = base64.b32encode(raw_hash).decode().rstrip("=")
        magnet = f"magnet:?xt=urn:btih:{encoded}&dn=Example"
        item = parse_magnet(magnet)

        self.assertEqual(item.info_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(item.display_name, "Example")

    def test_parse_torrent_single_file(self):
        info = {
            "name": "Example.Movie.2024.mkv",
            "length": 1024,
        }
        torrent = {
            "announce": "http://tracker.example/announce",
            "info": info,
        }
        payload = _bencode(torrent)
        expected_hash = hashlib.sha1(_bencode(info)).hexdigest()

        items = parse_torrent(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].info_hash, expected_hash)
        self.assertEqual(items[0].file_name, "Example.Movie.2024.mkv")
        self.assertEqual(items[0].file_idx, 0)
        self.assertEqual(items[0].sources, ["tracker:http://tracker.example/announce"])

    def test_parse_torrent_multi_file_keeps_video_index(self):
        info = {
            "name": "Example.Season.Pack",
            "files": [
                {"length": 100, "path": ["readme.txt"]},
                {"length": 2048, "path": ["Episode.S01E01.mkv"]},
            ],
        }
        torrent = {
            "announce-list": [["http://tracker.example/announce"]],
            "info": info,
        }
        payload = _bencode(torrent)
        expected_hash = hashlib.sha1(_bencode(info)).hexdigest()

        items = parse_torrent(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].info_hash, expected_hash)
        self.assertEqual(items[0].file_name, "Episode.S01E01.mkv")
        self.assertEqual(items[0].file_idx, 1)
        self.assertEqual(items[0].sources, ["tracker:http://tracker.example/announce"])


if __name__ == "__main__":
    unittest.main()
