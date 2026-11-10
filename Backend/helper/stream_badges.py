"""Text badges for stream rows, from the same rules as the Prism image pack.

Nuvio only draws image badges from packs a user imports, and addons cannot send any. So the server
runs the pack's own regexes on the file name and writes the result as two short text lines that
every app shows: quality ("4K · DV · HDR10 · REMUX · Atmos · 7.1 · HEVC") and languages.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

_PACK = json.loads((Path(__file__).resolve().parents[1] / "assets" / "badges" / "prism.json").read_text(encoding="utf-8"))

#----- Most important first, so the line reads the same way on every row
_QUALITY_GROUPS = ("resolution", "hdr", "picture", "source", "audio", "channels", "codec", "edition")

#----- Pack names are written for image captions; text rows need short ones
_SHORT = {
    "8k": "8K", "4k": "4K", "1440p": "1440p", "1080p": "1080p", "720p": "720p", "576p": "576p",
    "480p": "480p", "lowres": "Low res",
    "dolby-vision": "DV", "hdr10plus": "HDR10+", "hdr10": "HDR10", "hdr": "HDR", "hlg": "HLG",
    "fake-hdr": "⚠ Fake HDR",
    "imax-enhanced": "IMAX Enhanced", "hfr": "HFR",
    "atmos": "Atmos", "dtsx": "DTS:X", "truehd": "TrueHD", "dts-hd-ma": "DTS-HD MA", "dts-hd-hra": "DTS-HD HRA",
    "dts-hd": "DTS-HD", "ddp": "DD+", "dd": "DD", "dts-es": "DTS-ES", "dts": "DTS",
    "7.1": "7.1", "6.1": "6.1", "5.1": "5.1", "2.0": "2.0", "1.0": "Mono",
    "remux": "REMUX", "full-disc": "Full Disc", "uhd-bluray": "UHD BluRay", "bluray": "BluRay",
    "dvd-remux": "DVD REMUX", "webdl": "WEB-DL", "web": "WEB", "webrip": "WEBRip",
    "cam": "⚠ CAM", "telesync": "⚠ Telesync", "telecine": "⚠ Telecine", "screener": "⚠ Screener",
    "hardsub": "Hardsub",
    "av1": "AV1", "vvc": "VVC", "hevc": "HEVC", "avc": "AVC", "xvid": "XviD",
    "directors-cut": "Director's Cut", "special": "Special Edition",
}
#----- Detail that only clutters a one-line summary
_SKIP = {"8bit", "10bit", "12bit", "sdr", "dv-only", "dv-p7", "dv-p8"}


def _compile(groups):
    rules = []
    for f in _PACK["filters"]:
        if f.get("isEnabled", True) and f["groupId"] in groups and f["id"] not in _SKIP:
            try:
                rules.append((f["groupId"], _SHORT.get(f["id"], f["name"]), re.compile(f["pattern"])))
            except re.error:
                continue      # a pattern Python cannot read is skipped, not fatal
    return rules


_QUALITY = sorted(_compile(set(_QUALITY_GROUPS)), key=lambda r: _QUALITY_GROUPS.index(r[0]))   # stable: pack order inside a group
_LANGUAGE = _compile({"language"})


def _matches(rules, text):
    seen, out = set(), []
    for _, label, rx in rules:
        if label not in seen and rx.search(text):
            seen.add(label)
            out.append(label)
    return out


#----- (quality_line, language_line); either is "" when nothing matched. Cached: ~200 regexes per name.
@lru_cache(maxsize=4096)
def badge_lines(filename: str) -> tuple[str, str]:
    #----- The pack's rules expect spaced titles; "Atmos.7.1.ITA" -> "Atmos 7.1 ITA" (dots inside numbers stay)
    text = re.sub(r"(?<=\d)_(?=\d)", ".", filename or "")          # DD5_1 -> DD5.1
    text = re.sub(r"[._](?!\d)|(?<!\d)[._]", " ", text)
    return " · ".join(_matches(_QUALITY, text)), " · ".join(_matches(_LANGUAGE, text))
