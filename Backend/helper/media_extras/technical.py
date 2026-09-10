from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Optional

from pyrogram import Client
from pyrogram.types import Message

from Backend.logger import LOGGER
from Backend.helper.settings_manager import SettingsManager

try:
    from pymediainfo import MediaInfo
    _MEDIAINFO_OK = MediaInfo.can_parse()
except Exception:
    MediaInfo = None
    _MEDIAINFO_OK = False


def _normalize_codec(name: str | None) -> str:
    if not name:
        return ""
    n = str(name).lower().replace(" ", "")
    mapping = {
        "avc": "H.264", "avc1": "H.264", "h264": "H.264", "x264": "H.264",
        "hevc": "H.265", "h265": "H.265", "x265": "H.265", "hev1": "H.265",
        "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
        "aac": "AAC", "ac3": "AC3", "e-ac-3": "EAC3", "eac3": "EAC3",
        "truehd": "TrueHD", "mlp": "TrueHD",
        "dts": "DTS", "dtshd": "DTS-HD", "dts-hd": "DTS-HD",
        "flac": "FLAC", "opus": "Opus", "mp3": "MP3", "pcm": "PCM",
    }
    for k, v in mapping.items():
        if k in n:
            return v
    return str(name).upper()


def _hdr_from_track(t) -> str:
    hdr = (getattr(t, "hdr_format", None) or getattr(t, "hdr_format_commercial", None) or "")
    hdr = str(hdr).upper()
    if "DOLBY" in hdr or "VISION" in hdr or "DV" in hdr:
        return "DV"
    if "HDR10+" in hdr or "HDR10 PLUS" in hdr:
        return "HDR10+"
    if "HDR10" in hdr or "HDR" in hdr:
        return "HDR10"
    if "HLG" in hdr:
        return "HLG"
    transfer = str(getattr(t, "transfer_characteristics", None) or getattr(t, "colour_primaries", None) or "").lower()
    if "smpte2084" in transfer or "pq" in transfer:
        return "HDR10"
    if "arib-std-b67" in transfer or "hlg" in transfer:
        return "HLG"
    return ""


def _res_label(height: int | None) -> str:
    if not height:
        return ""
    if height >= 2160:
        return "2160p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    return f"{height}p"


def parse_mediainfo(path: str | Path) -> Optional[dict]:
    if not _MEDIAINFO_OK or MediaInfo is None:
        return None
    try:
        mi = MediaInfo.parse(str(path))
    except Exception as e:
        LOGGER.warning(f"MediaInfo.parse failed: {e}")
        return None

    result: dict[str, Any] = {
        "video": {},
        "audio": [],
        "subtitle": [],
        "duration": None,
        "bitrate": None,
        "container": None,
        "source": "mediainfo",
    }

    for t in mi.general_tracks:
        result["container"] = getattr(t, "format", None) or result["container"]
        dur = getattr(t, "duration", None)
        if dur is not None:
            try:
                result["duration"] = float(dur) / 1000.0
            except (TypeError, ValueError):
                pass
        br = getattr(t, "overall_bit_rate", None) or getattr(t, "bit_rate", None)
        if br is not None:
            try:
                result["bitrate"] = int(br)
            except (TypeError, ValueError):
                pass

    for t in mi.video_tracks:
        width = getattr(t, "width", None)
        height = getattr(t, "height", None)
        try:
            width = int(width) if width else None
            height = int(height) if height else None
        except (TypeError, ValueError):
            width = height = None
        fps = getattr(t, "frame_rate", None)
        try:
            fps = round(float(fps), 3) if fps else None
        except (TypeError, ValueError):
            fps = None
        bit_depth = getattr(t, "bit_depth", None)
        try:
            bit_depth = int(bit_depth) if bit_depth else None
        except (TypeError, ValueError):
            bit_depth = None
        codec = _normalize_codec(
            getattr(t, "format", None) or getattr(t, "codec_id", None) or getattr(t, "commercial_name", None)
        )
        br = getattr(t, "bit_rate", None)
        try:
            br = int(br) if br else None
        except (TypeError, ValueError):
            br = None
        result["video"] = {
            "codec": codec,
            "profile": getattr(t, "format_profile", None) or getattr(t, "codec_profile", None) or "",
            "width": width,
            "height": height,
            "resolution": f"{width}x{height}" if width and height else "",
            "resolution_label": _res_label(height),
            "fps": fps,
            "bit_depth": bit_depth,
            "pix_fmt": getattr(t, "color_space", None) or "",
            "hdr": _hdr_from_track(t),
            "bitrate": br,
        }
        break

    for t in mi.audio_tracks:
        ch = getattr(t, "channel_s", None)
        try:
            ch = int(ch) if ch else None
        except (TypeError, ValueError):
            ch = None
        br = getattr(t, "bit_rate", None)
        try:
            br = int(br) if br else None
        except (TypeError, ValueError):
            br = None
        result["audio"].append({
            "codec": _normalize_codec(
                getattr(t, "format", None) or getattr(t, "codec_id", None) or getattr(t, "commercial_name", None)
            ),
            "channels": ch,
            "channel_layout": getattr(t, "channel_layout", None) or "",
            "language": (getattr(t, "language", None) or "").lower(),
            "title": getattr(t, "title", None) or "",
            "bitrate": br,
        })

    for t in mi.text_tracks:
        forced = str(getattr(t, "forced", None) or getattr(t, "force_style", None) or "").lower()
        result["subtitle"].append({
            "codec": getattr(t, "format", None) or getattr(t, "codec_id", None) or "",
            "language": (getattr(t, "language", None) or "").lower(),
            "title": getattr(t, "title", None) or "",
            "forced": forced in ("1", "true", "yes"),
        })

    return result


def build_from_filename(filename: str) -> dict:
    import PTN
    try:
        parsed = PTN.parse(filename or "")
    except Exception:
        parsed = {}
    video = {}
    if parsed.get("resolution"):
        video["resolution_label"] = str(parsed["resolution"])
    if parsed.get("codec"):
        video["codec"] = _normalize_codec(str(parsed["codec"]))
    if parsed.get("bitDepth"):
        try:
            video["bit_depth"] = int(parsed["bitDepth"])
        except Exception:
            pass
    audio = []
    if parsed.get("audio"):
        audio.append({"codec": str(parsed["audio"]), "channels": None, "language": "", "title": ""})
    return {
        "video": video,
        "audio": audio,
        "subtitle": [],
        "duration": None,
        "bitrate": None,
        "container": None,
        "source": "filename",
        "encoder": parsed.get("encoder") or "",
        "quality": parsed.get("quality") or "",
    }


def merge_technical(base: dict, probe: dict | None) -> dict:
    if not probe:
        return base
    out = dict(base)
    out["source"] = probe.get("source") or "mediainfo"
    if probe.get("video"):
        v = dict(out.get("video") or {})
        v.update({k: v2 for k, v2 in probe["video"].items() if v2 not in (None, "", [])})
        out["video"] = v
    if probe.get("audio"):
        out["audio"] = probe["audio"]
    if probe.get("subtitle"):
        out["subtitle"] = probe["subtitle"]
    for k in ("duration", "bitrate", "container"):
        if probe.get(k) is not None:
            out[k] = probe[k]
    return out


_metadata_client: Client | None = None
_metadata_client_lock = asyncio.Lock()


async def get_metadata_client() -> Optional[Client]:
    global _metadata_client
    token = (SettingsManager.current().to_dict().get("metadata_bot_token") or "").strip()
    if not token:
        return None
    async with _metadata_client_lock:
        if _metadata_client is not None:
            try:
                if _metadata_client.is_connected:
                    return _metadata_client
            except Exception:
                pass
            try:
                await _metadata_client.stop()
            except Exception:
                pass
            _metadata_client = None
        from Backend.config import Telegram
        client = Client(
            name="metadata_bot",
            api_id=Telegram.API_ID,
            api_hash=Telegram.API_HASH,
            bot_token=token,
            in_memory=True,
            no_updates=True,
            sleep_threshold=30,
        )
        try:
            await client.start()
            _metadata_client = client
            LOGGER.info("Metadata bot client started")
            return client
        except Exception as e:
            LOGGER.error(f"Failed to start metadata bot: {e}")
            return None


async def stop_metadata_client() -> None:
    global _metadata_client
    async with _metadata_client_lock:
        if _metadata_client is not None:
            try:
                await _metadata_client.stop()
            except Exception:
                pass
            _metadata_client = None


async def download_probe_bytes(client: Client, message: Message, max_bytes: int) -> Optional[Path]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".probe")
    tmp_path = Path(tmp.name)
    tmp.close()
    written = 0
    try:
        if hasattr(client, "stream_media"):
            async for chunk in client.stream_media(message, limit=max_bytes):
                if not chunk:
                    break
                with open(tmp_path, "ab") as f:
                    f.write(chunk)
                written += len(chunk)
                if written >= max_bytes:
                    break
        else:
            path = await client.download_media(message, file_name=str(tmp_path))
            if path and Path(path).exists():
                size = Path(path).stat().st_size
                if size > max_bytes:
                    with open(path, "rb") as f:
                        data = f.read(max_bytes)
                    with open(tmp_path, "wb") as f:
                        f.write(data)
                    if str(path) != str(tmp_path):
                        Path(path).unlink(missing_ok=True)
                    written = len(data)
                else:
                    written = size
                    if str(path) != str(tmp_path):
                        Path(path).rename(tmp_path)
        if written < 1024:
            tmp_path.unlink(missing_ok=True)
            return None
        return tmp_path
    except Exception as e:
        LOGGER.warning(f"probe download failed: {e}")
        tmp_path.unlink(missing_ok=True)
        return None


async def extract_technical(
    chat_id: int,
    msg_id: int,
    filename: str = "",
    max_mb: float | None = None,
) -> dict:
    base = build_from_filename(filename)
    if not _MEDIAINFO_OK:
        return base

    client = await get_metadata_client()
    if client is None:
        return base

    settings = SettingsManager.current().to_dict()
    if max_mb is None:
        try:
            max_mb = float(settings.get("ffprobe_max_mb") or 8)
        except (TypeError, ValueError):
            max_mb = 8.0
    max_bytes = max(1, int(max_mb * 1024 * 1024))

    try:
        message = await client.get_messages(chat_id, msg_id)
        if not message or message.empty:
            return base
        path = await download_probe_bytes(client, message, max_bytes)
        if not path:
            return base
        try:
            probe = await asyncio.to_thread(parse_mediainfo, path)
            return merge_technical(base, probe)
        finally:
            path.unlink(missing_ok=True)
    except Exception as e:
        LOGGER.warning(f"extract_technical failed for {chat_id}/{msg_id}: {e}")
        return base
