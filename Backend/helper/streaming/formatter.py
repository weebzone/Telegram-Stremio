from __future__ import annotations

from typing import Any, Optional

PREDEFINED_TEMPLATES = {
    "default": {
        "name": "Telegram {resolution} {quality}",
        "title": "📁 {filename}\n{size_emoji} {size}\n{codec_line}",
    },
    "compact": {
        "name": "{resolution} {codec} {audio_short}",
        "title": "{filename}\n{size} · {video_codec} · {audio_summary}",
    },
    "detailed": {
        "name": "Telegram {resolution} {quality}",
        "title": (
            "📁 {filename}\n"
            "{size_emoji} {size}"
            "{video_line}"
            "{audio_line}"
            "{subs_line}"
            "{hdr_line}"
            "{encoder_line}"
        ),
    },
    "technical": {
        "name": "{resolution} | {video_codec} | {audio_short}",
        "title": (
            "{filename}\n"
            "Size: {size}\n"
            "Video: {video_codec} {resolution} {fps}fps {bit_depth}bit {hdr}\n"
            "Audio: {audio_summary}\n"
            "Subs: {subs_summary}"
        ),
    },
    "emoji": {
        "name": "🎬 {resolution} {quality}",
        "title": (
            "📁 {filename}\n"
            "💾 {size}\n"
            "🎥 {video_codec} {hdr}\n"
            "🔊 {audio_summary}\n"
            "💬 {subs_summary}"
        ),
    },
    "minimal": {
        "name": "{resolution}",
        "title": "{filename} · {size}",
    },
}

PLACEHOLDER_GUIDE = {
    "resolution": "e.g. 1080p, 2160p",
    "quality": "WEB-DL, BluRay, etc.",
    "filename": "Original file name",
    "size": "Human readable size",
    "size_emoji": "📦 or 💾",
    "codec": "Primary video codec",
    "video_codec": "Video codec (H.264 / H.265 / AV1)",
    "audio": "First audio codec",
    "audio_short": "Short audio string e.g. AAC 5.1",
    "audio_summary": "All audio tracks summary",
    "channels": "Audio channels of first track",
    "subs": "Subtitle languages short",
    "subs_summary": "Full subtitle list",
    "hdr": "HDR10 / DV / HLG or empty",
    "bit_depth": "8 / 10 / 12",
    "fps": "Frame rate",
    "bitrate": "Overall bitrate",
    "duration": "Duration string",
    "encoder": "Release group / encoder",
    "container": "mkv / mp4",
    "source": "ffprobe or filename",
    "codec_line": "Prebuilt codec line with emojis",
    "video_line": "Prebuilt video info line",
    "audio_line": "Prebuilt audio line",
    "subs_line": "Prebuilt subs line",
    "hdr_line": "Prebuilt HDR line",
    "encoder_line": "Prebuilt encoder line",
}


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    try:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m"
        return f"{m}m{sec:02d}s"
    except Exception:
        return ""


def _audio_short(audio: list) -> str:
    if not audio:
        return ""
    a = audio[0]
    parts = [a.get("codec") or ""]
    ch = a.get("channels")
    if ch:
        if ch >= 6:
            parts.append("5.1" if ch == 6 else f"{ch}ch")
        elif ch == 2:
            parts.append("2.0")
    return " ".join(p for p in parts if p)


def _audio_summary(audio: list) -> str:
    if not audio:
        return "None"
    parts = []
    for a in audio:
        bit = []
        if a.get("codec"):
            bit.append(a["codec"])
        if a.get("channels"):
            ch = a["channels"]
            bit.append("5.1" if ch == 6 else ("7.1" if ch == 8 else f"{ch}ch"))
        if a.get("language"):
            bit.append(a["language"].upper())
        if bit:
            parts.append(" ".join(bit))
    return " · ".join(parts) if parts else "None"


def _subs_summary(subs: list) -> str:
    if not subs:
        return "None"
    langs = []
    for s in subs:
        lang = (s.get("language") or "und").upper()
        if s.get("forced"):
            lang += " (forced)"
        langs.append(lang)
    return ", ".join(dict.fromkeys(langs))


def build_context(
    filename: str,
    quality: str,
    size: str,
    is_split: bool = False,
    technical: Optional[dict] = None,
) -> dict[str, str]:
    tech = technical or {}
    video = tech.get("video") or {}
    audio = tech.get("audio") or []
    subs = tech.get("subtitle") or []

    resolution = video.get("resolution_label") or quality or ""
    video_codec = video.get("codec") or ""
    hdr = video.get("hdr") or ""
    bit_depth = str(video.get("bit_depth") or "") if video.get("bit_depth") else ""
    fps = str(video.get("fps") or "") if video.get("fps") else ""
    encoder = tech.get("encoder") or ""
    q = tech.get("quality") or quality or ""

    size_emoji = "📦" if is_split else "💾"
    codec_parts = []
    if video_codec:
        codec_parts.append(f"🎥 {video_codec}")
    if bit_depth:
        codec_parts.append(f"🌈 {bit_depth}bit")
    if audio:
        codec_parts.append(f"🔊 {_audio_short(audio)}")
    if encoder:
        codec_parts.append(f"👤 {encoder}")
    codec_line = " ".join(codec_parts)

    video_line = ""
    if video_codec or resolution:
        video_line = f"\n🎥 {video_codec} {resolution} {f'{fps}fps' if fps else ''} {f'{bit_depth}bit' if bit_depth else ''}".strip()
    audio_line = f"\n🔊 {_audio_summary(audio)}" if audio else ""
    subs_line = f"\n💬 {_subs_summary(subs)}" if subs else ""
    hdr_line = f"\n🌈 {hdr}" if hdr else ""
    encoder_line = f"\n👤 {encoder}" if encoder else ""

    return {
        "resolution": resolution,
        "quality": q,
        "filename": filename or "",
        "size": size or "",
        "size_emoji": size_emoji,
        "codec": video_codec,
        "video_codec": video_codec,
        "audio": (audio[0].get("codec") if audio else "") or "",
        "audio_short": _audio_short(audio),
        "audio_summary": _audio_summary(audio),
        "channels": str(audio[0].get("channels") or "") if audio else "",
        "subs": _subs_summary(subs),
        "subs_summary": _subs_summary(subs),
        "hdr": hdr,
        "bit_depth": bit_depth,
        "fps": fps,
        "bitrate": str(tech.get("bitrate") or ""),
        "duration": _fmt_duration(tech.get("duration")),
        "encoder": encoder,
        "container": tech.get("container") or "",
        "source": tech.get("source") or "filename",
        "codec_line": codec_line,
        "video_line": video_line,
        "audio_line": audio_line,
        "subs_line": subs_line,
        "hdr_line": hdr_line,
        "encoder_line": encoder_line,
    }


def render_template(template: str, ctx: dict[str, str]) -> str:
    if not template:
        return ""
    out = template
    for key, val in ctx.items():
        out = out.replace("{" + key + "}", str(val or ""))
    out = out.replace("\n\n", "\n").strip()
    return out


def format_stream(
    filename: str,
    quality: str,
    size: str,
    is_split: bool = False,
    technical: Optional[dict] = None,
    name_template: Optional[str] = None,
    title_template: Optional[str] = None,
) -> tuple[str, str]:
    ctx = build_context(filename, quality, size, is_split, technical)
    name_t = name_template or PREDEFINED_TEMPLATES["default"]["name"]
    title_t = title_template or PREDEFINED_TEMPLATES["default"]["title"]
    name = render_template(name_t, ctx).strip() or f"Telegram {quality}"
    title = render_template(title_t, ctx).strip() or f"📁 {filename}\n{size}"
    return name, title


def get_templates_for_token(token_config: dict | None, global_settings: dict) -> tuple[str, str]:
    cfg = token_config or {}
    name = (cfg.get("stream_name_template") or "").strip()
    title = (cfg.get("stream_title_template") or "").strip()
    if not name:
        name = (global_settings.get("stream_name_template") or "").strip()
    if not title:
        title = (global_settings.get("stream_title_template") or "").strip()
    if not name:
        name = PREDEFINED_TEMPLATES["default"]["name"]
    if not title:
        title = PREDEFINED_TEMPLATES["default"]["title"]
    return name, title
