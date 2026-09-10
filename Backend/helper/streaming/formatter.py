from __future__ import annotations

import re
from typing import Any, Optional

PREDEFINED_TEMPLATES = {
    "default": {
        "name": "Telegram {resolution} {quality}",
        "title": "📁 {filename}\n{size_emoji} {size}{video_line}{audio_line}{subs_line}",
    },
    "compact": {
        "name": "{resolution} {video_codec} {audio_short}",
        "title": "{filename}\n{size}{video_codec::exists[\" · {video_codec}\"||\"\"]}{audio_short::exists[\" · {audio_short}\"||\"\"]}",
    },
    "detailed": {
        "name": "Telegram {resolution} {quality}",
        "title": "📁 {filename}\n{size_emoji} {size}{video_line}{audio_line}{subs_line}{encoder_line}",
    },
    "emoji": {
        "name": "🎬 {resolution} {quality}",
        "title": "📁 {filename}\n💾 {size}{video_line}{audio_line}{subs_line}",
    },
    "minimal": {
        "name": "{resolution}",
        "title": "{filename} · {size}",
    },
}

_OLD_NAME = "Telegram {resolution} {quality}"
_OLD_TITLE = "📁 {filename}\n{size_emoji} {size}{codec_line::exists[\"\n{codec_line}\"||\"\"]}"


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    try:
        s = int(float(seconds))
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
        if ch >= 8:
            parts.append("7.1")
        elif ch >= 6:
            parts.append("5.1")
        elif ch == 2:
            parts.append("2.0")
        else:
            parts.append(f"{ch}ch")
    return " ".join(p for p in parts if p)


def _audio_summary(audio: list) -> str:
    if not audio:
        return ""
    parts = []
    for a in audio:
        bit = []
        if a.get("codec"):
            bit.append(a["codec"])
        ch = a.get("channels")
        if ch:
            bit.append("7.1" if ch >= 8 else ("5.1" if ch >= 6 else ("2.0" if ch == 2 else f"{ch}ch")))
        if a.get("language"):
            bit.append(str(a["language"]).upper())
        if bit:
            parts.append(" ".join(bit))
    return " · ".join(parts)


def _audio_langs(audio: list) -> str:
    langs = []
    for a in audio or []:
        lang = (a.get("language") or "").strip()
        if lang and lang not in ("und", "unknown"):
            langs.append(lang.upper())
        elif a.get("codec"):
            langs.append(a["codec"])
    return ", ".join(dict.fromkeys(langs))


def _subs_summary(subs: list) -> str:
    if not subs:
        return ""
    langs = []
    for s in subs:
        lang = (s.get("language") or "und").upper()
        if s.get("forced"):
            lang += " (forced)"
        langs.append(lang)
    return ", ".join(dict.fromkeys(langs))


def _sub_langs(subs: list) -> str:
    langs = []
    for s in subs or []:
        lang = (s.get("language") or "").strip()
        if lang and lang not in ("und", "unknown"):
            if s.get("forced"):
                lang = f"{lang} (forced)"
            langs.append(lang.upper())
    return ", ".join(dict.fromkeys(langs))


def _truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, (list, tuple, dict, set)):
        return len(val) > 0
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    return s not in ("", "none", "null", "false", "0")


def _resolve_path(ctx: dict, path: str) -> Any:
    path = path.strip()
    if path in ctx:
        return ctx[path]
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _apply_filters(val: Any, filters: list[str], ctx: dict) -> Any:
    for f in filters:
        f = f.strip()
        if not f:
            continue
        if f.startswith("default(") and f.endswith(")"):
            arg = f[8:-1].strip().strip("'\"")
            if not _truthy(val):
                val = arg
        elif f == "exists":
            val = _truthy(val)
        elif f == "length":
            if isinstance(val, (list, tuple, dict, str)):
                val = len(val)
            else:
                val = 0 if not _truthy(val) else 1
        elif f.startswith("join(") and f.endswith(")"):
            sep = f[5:-1].strip().strip("'\"")
            if isinstance(val, (list, tuple)):
                val = sep.join(str(x) for x in val if _truthy(x))
            elif _truthy(val):
                val = str(val)
            else:
                val = ""
        elif f.startswith("replace(") and f.endswith(")"):
            inner = f[8:-1]
            parts = [p.strip().strip("'\"") for p in inner.split(",")]
            if len(parts) >= 2 and isinstance(val, str):
                val = val.replace(parts[0], parts[1])
        elif f.startswith("="):
            expected = f[1:].strip().strip("'\"")
            val = str(val or "") == expected
        elif f.startswith(">"):
            try:
                val = float(val or 0) > float(f[1:].strip())
            except (TypeError, ValueError):
                val = False
        elif f.startswith("<"):
            try:
                val = float(val or 0) < float(f[1:].strip())
            except (TypeError, ValueError):
                val = False
        elif f == "upper":
            val = str(val or "").upper()
        elif f == "lower":
            val = str(val or "").lower()
        elif f == "bytes2":
            try:
                n = float(val)
                for unit in ("B", "KB", "MB", "GB", "TB"):
                    if n < 1024 or unit == "TB":
                        val = f"{n:.2f}{unit}" if unit != "B" else f"{int(n)}B"
                        break
                    n /= 1024
            except (TypeError, ValueError):
                pass
    return val


def _split_ternary(body: str) -> tuple[str, str] | None:
    depth = 0
    in_str = None
    i = 0
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\" and i + 1 < len(body):
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            i += 1
            continue
        if c in "[{(":
            depth += 1
        elif c in "]})":
            depth -= 1
        elif c == "|" and i + 1 < len(body) and body[i + 1] == "|" and depth == 0:
            return body[:i], body[i + 2:]
        i += 1
    return None


def _strip_quotes(s: str) -> str:
    s = s.strip(" \t")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _eval_token(expr: str, ctx: dict) -> str:
    expr = expr.strip()
    ternary = None
    if "[" in expr and expr.endswith("]"):
        bracket_at = expr.rfind("[")
        head = expr[:bracket_at]
        body = expr[bracket_at + 1:-1]
        parts = _split_ternary(body)
        if parts:
            ternary = parts
            expr = head

    segments = [s.strip() for s in expr.split("::") if s.strip()]
    if not segments:
        return ""

    path = segments[0]
    filters = segments[1:]
    val = _resolve_path(ctx, path)
    val = _apply_filters(val, filters, ctx)

    if ternary is not None:
        yes_t = _strip_quotes(ternary[0])
        no_t = _strip_quotes(ternary[1])
        chosen = yes_t if _truthy(val) else no_t
        return render_template(chosen, ctx, clean=False) if chosen else ""

    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else ""
    if isinstance(val, (list, tuple)):
        return ", ".join(str(x) for x in val if _truthy(x))
    s = str(val)
    if s.lower() in ("none", "null"):
        return ""
    return s


def _find_tokens(template: str):
    result = []
    i = 0
    n = len(template)
    while i < n:
        if template[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = None
        while j < n:
            c = template[j]
            if in_str:
                if c == "\\" and j + 1 < n:
                    j += 2
                    continue
                if c == in_str:
                    in_str = None
                j += 1
                continue
            if c in ("'", '"'):
                in_str = c
                j += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    result.append((i, j + 1, template[i + 1:j]))
                    i = j + 1
                    break
            j += 1
        else:
            break
    return result


def render_template(template: str, ctx: dict, clean: bool = True) -> str:
    if not template:
        return ""
    for _ in range(12):
        tokens = _find_tokens(template)
        if not tokens:
            break
        parts = []
        last = 0
        for start, end, expr in tokens:
            parts.append(template[last:start])
            try:
                parts.append(_eval_token(expr, ctx))
            except Exception:
                parts.append("")
            last = end
        parts.append(template[last:])
        template = "".join(parts)
    if not clean:
        return template
    lines = []
    for line in template.split("\n"):
        cleaned = re.sub(r"[ \t]{2,}", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines).strip()


def _has_real_technical(technical: Optional[dict]) -> bool:
    if not technical:
        return False
    if technical.get("source") == "mediainfo":
        return True
    video = technical.get("video") or {}
    if video.get("codec") and video.get("width"):
        return True
    if technical.get("audio") or technical.get("subtitle"):
        return True
    return False


def build_context(
    filename: str,
    quality: str,
    size: str,
    is_split: bool = False,
    technical: Optional[dict] = None,
    media_title: str = "",
    season_episode: str = "",
    addon_name: str = "Telegram",
) -> dict:
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

    specs = [x for x in [resolution, video_codec, hdr, _audio_short(audio)] if x]

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
        bits = [x for x in [video_codec, resolution, f"{fps}fps" if fps else "", f"{bit_depth}bit" if bit_depth else "", hdr] if x]
        if bits:
            video_line = "\n🎥 " + " ".join(bits)
    audio_line = f"\n🔊 {_audio_summary(audio)}" if audio else ""
    subs_line = f"\n💬 {_subs_summary(subs)}" if subs else ""
    encoder_line = f"\n👤 {encoder}" if encoder else ""

    langs_list = []
    for a in audio:
        lang = (a.get("language") or "").strip()
        if lang and lang not in ("und", "unknown"):
            langs_list.append(lang.upper())
    sub_list = []
    for s in subs:
        lang = (s.get("language") or "").strip()
        if lang and lang not in ("und", "unknown"):
            sub_list.append(lang.upper() + (" (forced)" if s.get("forced") else ""))

    return {
        "resolution": resolution,
        "quality": q,
        "filename": filename or "",
        "size": size or "",
        "size_emoji": size_emoji,
        "size_bytes": tech.get("size_bytes") or 0,
        "codec": video_codec,
        "video_codec": video_codec,
        "audio": (audio[0].get("codec") if audio else "") or "",
        "audio_short": _audio_short(audio),
        "audio_summary": _audio_summary(audio),
        "audio_langs": _audio_langs(audio),
        "channels": str(audio[0].get("channels") or "") if audio else "",
        "subs": _subs_summary(subs),
        "subs_summary": _subs_summary(subs),
        "sub_langs": _sub_langs(subs),
        "hdr": hdr,
        "bit_depth": bit_depth,
        "fps": fps,
        "bitrate": str(tech.get("bitrate") or "") if tech.get("bitrate") else "",
        "duration": _fmt_duration(tech.get("duration")),
        "encoder": encoder,
        "container": tech.get("container") or "",
        "source": tech.get("source") or "filename",
        "codec_line": codec_line,
        "video_line": video_line,
        "audio_line": audio_line,
        "subs_line": subs_line,
        "encoder_line": encoder_line,
        "hdr_line": f"\n🌈 {hdr}" if hdr else "",
        "specs": specs,
        "languages": langs_list,
        "subtitles": sub_list,
        "seasonEpisode": season_episode or "",
        "title": media_title or filename or "",
        "addon_name": addon_name,
        "stream": {
            "resolution": resolution,
            "quality": q,
            "filename": filename or "",
            "size": size or "",
            "size_bytes": tech.get("size_bytes") or 0,
            "video_codec": video_codec,
            "audio_short": _audio_short(audio),
            "audio_summary": _audio_summary(audio),
            "audio_langs": _audio_langs(audio),
            "specs": specs,
            "languages": langs_list,
            "subtitles": sub_list,
            "hdr": hdr,
            "bit_depth": bit_depth,
            "fps": fps,
            "seasonEpisode": season_episode or "",
            "streamType": video_codec or "HLS",
        },
        "metadata": {"title": media_title or ""},
        "addon": {"name": addon_name},
    }


def format_stream(
    filename: str,
    quality: str,
    size: str,
    is_split: bool = False,
    technical: Optional[dict] = None,
    name_template: Optional[str] = None,
    title_template: Optional[str] = None,
    media_title: str = "",
    season_episode: str = "",
    addon_name: str = "Telegram",
) -> tuple[str, str]:
    ctx = build_context(
        filename, quality, size, is_split, technical,
        media_title=media_title, season_episode=season_episode, addon_name=addon_name,
    )

    if not _has_real_technical(technical):
        name = render_template(_OLD_NAME, ctx).strip() or f"Telegram {quality}"
        title = render_template(_OLD_TITLE, ctx).strip() or f"📁 {filename}\n{size}"
        return name, title

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
