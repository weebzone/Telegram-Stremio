"""Self-hosted Nuvio "Fusion" badge pack (Prism style from jyotiprakash101/nuvio-badge-kit, MIT).

Nuvio only shows image badges from packs a user imports once (Profile > Layout > Streams >
Fusion badge URLs); it does not read badges from addons. This serves the pack from this server so
that one link is all users need and nothing depends on GitHub after the first download.
"""
import copy
import os
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from Backend.helper.settings_manager import SettingsManager
from Backend.helper.stream_badges import _PACK
from Backend.logger import LOGGER

router = APIRouter()

CACHE_DIR = Path(os.getenv("BADGE_CACHE_DIR", "cache/badges/prism"))
_UPSTREAM = "https://raw.githubusercontent.com/jyotiprakash101/nuvio-badge-kit/964d2e70c480fee71ef7e3ee6fa21562cd6364ca/v2/Prism/"

_IMAGES = {f["imageURL"].rsplit("/", 1)[1] for f in _PACK["filters"]}   # the only names we ever fetch
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.webp$")
_pack_cache: dict = {}


def _base_url(request: Request) -> str:
    return (SettingsManager.current().base_url or str(request.base_url)).rstrip("/")


@router.get("/badges/prism.json")
async def prism_pack(request: Request):
    base = _base_url(request)
    pack = _pack_cache.get(base)
    if pack is None:
        pack = copy.deepcopy(_PACK)
        for f in pack["filters"]:
            f["imageURL"] = f"{base}/badges/prism/{f['imageURL'].rsplit('/', 1)[1]}"
        _pack_cache[base] = pack
    return JSONResponse(pack, headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"})


async def _download(name: str) -> bytes:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(_UPSTREAM + name)
        r.raise_for_status()
        return r.content


@router.get("/badges/prism/{name}")
async def prism_image(name: str):
    if name not in _IMAGES or not _NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="Unknown badge")
    path = CACHE_DIR / name
    if not path.exists():
        try:
            data = await _download(name)
        except Exception as e:
            LOGGER.warning(f"[Badges] Could not download {name}: {e}")
            raise HTTPException(status_code=502, detail="Badge temporarily unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)          # atomic, so a half-written file is never served
    return FileResponse(path, media_type="image/webp", headers={
        "Cache-Control": "public, max-age=2592000, immutable", "Access-Control-Allow-Origin": "*"})
