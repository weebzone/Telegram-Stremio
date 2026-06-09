import asyncio
import random
import socket
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_from_bytes, urlparse, urlunparse

import httpx


UDP_TRACKER_PROTOCOL_ID = 0x41727101980


@dataclass(frozen=True)
class TrackerScrapeResult:
    tracker: str
    seeders: int = 0
    peers: int = 0
    completed: int = 0
    ok: bool = False
    error: str = ""


class BencodeParser:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def parse(self, pos: int = 0):
        if pos >= len(self.data):
            raise ValueError("Unexpected end of bencode data")
        token = self.data[pos:pos + 1]
        if token == b"i":
            return self._parse_int(pos)
        if token == b"l":
            return self._parse_list(pos)
        if token == b"d":
            return self._parse_dict(pos)
        if token.isdigit():
            return self._parse_bytes(pos)
        raise ValueError(f"Invalid bencode token at {pos}")

    def _parse_int(self, pos: int):
        end = self.data.index(b"e", pos)
        return int(self.data[pos + 1:end]), end + 1

    def _parse_bytes(self, pos: int):
        colon = self.data.index(b":", pos)
        length = int(self.data[pos:colon])
        start = colon + 1
        end = start + length
        return self.data[start:end], end

    def _parse_list(self, pos: int):
        pos += 1
        out = []
        while self.data[pos:pos + 1] != b"e":
            item, pos = self.parse(pos)
            out.append(item)
        return out, pos + 1

    def _parse_dict(self, pos: int):
        pos += 1
        out = {}
        while self.data[pos:pos + 1] != b"e":
            key, pos = self._parse_bytes(pos)
            value, pos = self.parse(pos)
            out[key] = value
        return out, pos + 1


def _info_hash_bytes(info_hash: str) -> bytes:
    value = str(info_hash or "").strip().lower()
    if len(value) != 40:
        raise ValueError("info_hash must be a 40-character hex string")
    return bytes.fromhex(value)


def normalize_tracker_source(source: str) -> str:
    value = str(source or "").strip()
    if value.startswith("tracker:"):
        value = value[len("tracker:"):]
    return value.strip()


def dedupe_trackers(sources: list[str], max_trackers: int) -> list[str]:
    out = []
    seen = set()
    for source in sources or []:
        tracker = normalize_tracker_source(source)
        if not tracker or tracker in seen:
            continue
        parsed = urlparse(tracker)
        if parsed.scheme not in {"http", "https", "udp"}:
            continue
        seen.add(tracker)
        out.append(tracker)
        if max_trackers > 0 and len(out) >= max_trackers:
            break
    return out


def http_scrape_url(announce_url: str) -> str | None:
    parsed = urlparse(announce_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    idx = parsed.path.rfind("announce")
    if idx < 0:
        return None
    new_path = parsed.path[:idx] + "scrape" + parsed.path[idx + len("announce"):]
    return urlunparse(parsed._replace(path=new_path))


def parse_http_scrape_response(data: bytes, info_hash: str) -> TrackerScrapeResult:
    target = _info_hash_bytes(info_hash)
    root, pos = BencodeParser(data).parse(0)
    if pos != len(data):
        raise ValueError("Trailing data after tracker scrape response")
    if not isinstance(root, dict):
        raise ValueError("Invalid tracker scrape response")
    if b"failure reason" in root:
        raise ValueError(root.get(b"failure reason", b"").decode("utf-8", errors="replace"))

    files = root.get(b"files")
    if not isinstance(files, dict):
        raise ValueError("Tracker scrape response has no files dictionary")
    stats = files.get(target)
    if not isinstance(stats, dict):
        raise ValueError("Tracker scrape response has no stats for info hash")

    return TrackerScrapeResult(
        tracker="",
        seeders=max(0, int(stats.get(b"complete", 0) or 0)),
        peers=max(0, int(stats.get(b"incomplete", 0) or 0)),
        completed=max(0, int(stats.get(b"downloaded", 0) or 0)),
        ok=True,
    )


def decode_udp_scrape_response(data: bytes, transaction_id: int) -> TrackerScrapeResult:
    if len(data) < 8:
        raise ValueError("UDP scrape response too short")
    action, tx_id = struct.unpack_from(">II", data, 0)
    if tx_id != transaction_id:
        raise ValueError("UDP scrape transaction mismatch")
    if action == 3:
        raise ValueError(data[8:].decode("utf-8", errors="replace") or "UDP tracker error")
    if action != 2:
        raise ValueError("Unexpected UDP tracker action")
    if len(data) < 20:
        raise ValueError("UDP scrape response missing stats")
    seeders, completed, peers = struct.unpack_from(">III", data, 8)
    return TrackerScrapeResult(
        tracker="",
        seeders=max(0, int(seeders)),
        peers=max(0, int(peers)),
        completed=max(0, int(completed)),
        ok=True,
    )


async def scrape_http_tracker(tracker: str, info_hash: str, timeout: float) -> TrackerScrapeResult:
    scrape_url = http_scrape_url(tracker)
    if not scrape_url:
        return TrackerScrapeResult(tracker=tracker, error="scrape_url_unavailable")

    separator = "&" if urlparse(scrape_url).query else "?"
    url = f"{scrape_url}{separator}info_hash={quote_from_bytes(_info_hash_bytes(info_hash))}"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Telegram-Stremio/2.1"})
            response.raise_for_status()
        result = parse_http_scrape_response(response.content, info_hash)
        return TrackerScrapeResult(
            tracker=tracker,
            seeders=result.seeders,
            peers=result.peers,
            completed=result.completed,
            ok=True,
        )
    except Exception as e:
        return TrackerScrapeResult(tracker=tracker, error=str(e)[:160])


async def scrape_udp_tracker(tracker: str, info_hash: str, timeout: float) -> TrackerScrapeResult:
    parsed = urlparse(tracker)
    if parsed.scheme != "udp" or not parsed.hostname:
        return TrackerScrapeResult(tracker=tracker, error="invalid_udp_tracker")

    port = parsed.port or 80
    loop = asyncio.get_running_loop()
    sock = None
    try:
        addr_info = await loop.getaddrinfo(
            parsed.hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_DGRAM,
        )
        if not addr_info:
            raise ValueError("tracker_dns_failed")

        family, socktype, proto, _, sockaddr = addr_info[0]
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)

        tx_connect = random.randint(0, 0xFFFFFFFF)
        connect_packet = struct.pack(">QII", UDP_TRACKER_PROTOCOL_ID, 0, tx_connect)
        await loop.sock_sendto(sock, connect_packet, sockaddr)
        data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout=timeout)
        if len(data) < 16:
            raise ValueError("UDP connect response too short")
        action, tx_id, connection_id = struct.unpack_from(">IIQ", data, 0)
        if action == 3:
            raise ValueError(data[8:].decode("utf-8", errors="replace") or "UDP tracker error")
        if action != 0 or tx_id != tx_connect:
            raise ValueError("UDP connect transaction mismatch")

        tx_scrape = random.randint(0, 0xFFFFFFFF)
        scrape_packet = struct.pack(">QII20s", connection_id, 2, tx_scrape, _info_hash_bytes(info_hash))
        await loop.sock_sendto(sock, scrape_packet, sockaddr)
        data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), timeout=timeout)
        result = decode_udp_scrape_response(data, tx_scrape)
        return TrackerScrapeResult(
            tracker=tracker,
            seeders=result.seeders,
            peers=result.peers,
            completed=result.completed,
            ok=True,
        )
    except Exception as e:
        return TrackerScrapeResult(tracker=tracker, error=str(e)[:160])
    finally:
        if sock is not None:
            sock.close()


async def scrape_tracker(tracker: str, info_hash: str, timeout: float) -> TrackerScrapeResult:
    scheme = urlparse(tracker).scheme
    if scheme in {"http", "https"}:
        return await scrape_http_tracker(tracker, info_hash, timeout)
    if scheme == "udp":
        return await scrape_udp_tracker(tracker, info_hash, timeout)
    return TrackerScrapeResult(tracker=tracker, error="unsupported_tracker_scheme")


def aggregate_tracker_stats(results: list[TrackerScrapeResult], trackers_checked: int) -> dict[str, Any]:
    good = [result for result in results if result.ok]
    if not good:
        return {
            "seeders": None,
            "peers": None,
            "completed": None,
            "trackers_checked": trackers_checked,
            "trackers_responded": 0,
            "status": "unavailable",
            "errors": [result.error for result in results if result.error][:5],
        }

    return {
        "seeders": max(result.seeders for result in good),
        "peers": max(result.peers for result in good),
        "completed": max(result.completed for result in good),
        "trackers_checked": trackers_checked,
        "trackers_responded": len(good),
        "status": "ok",
        "errors": [result.error for result in results if result.error][:5],
    }


async def scrape_torrent_trackers(
    info_hash: str,
    sources: list[str],
    max_trackers: int = 5,
    timeout: float = 2.5,
    concurrency: int = 3,
) -> dict[str, Any]:
    trackers = dedupe_trackers(sources, max_trackers)
    if not trackers:
        return aggregate_tracker_stats([], trackers_checked=0)

    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def run_one(tracker: str) -> TrackerScrapeResult:
        async with sem:
            return await scrape_tracker(tracker, info_hash, timeout)

    results = await asyncio.gather(*[run_one(tracker) for tracker in trackers], return_exceptions=True)
    normalized = [
        result if isinstance(result, TrackerScrapeResult)
        else TrackerScrapeResult(tracker="", error=str(result)[:160])
        for result in results
    ]
    return aggregate_tracker_stats(normalized, trackers_checked=len(trackers))
