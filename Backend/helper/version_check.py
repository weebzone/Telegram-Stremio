import asyncio
import re
from typing import Optional

import httpx

from Backend import __version__
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
_state = {
    "latest": None,
    "update_available": False,
    "checked_at": 0.0,
    "error": "",
}
_lock = asyncio.Lock()
_CHECK_INTERVAL = 12 * 3600


def _parse_repo(url: str) -> Optional[tuple[str, str]]:
    if not url:
        return None
    url = url.strip().rstrip("/")
    if "github.com" in url:
        parts = url.split("github.com/")[-1].split("/")
        if len(parts) >= 2:
            return parts[0], parts[1].removesuffix(".git")
    return None


def _cmp_version(a: str, b: str) -> int:
    def parts(v):
        out = []
        for p in re.split(r"[.\-+]", v):
            out.append(int(p) if p.isdigit() else p)
        return out
    pa, pb = parts(a), parts(b)
    for x, y in zip(pa, pb):
        if type(x) != type(y):
            x, y = str(x), str(y)
        if x < y:
            return -1
        if x > y:
            return 1
    return (len(pa) > len(pb)) - (len(pa) < len(pb))


async def check_upstream_version(force: bool = False) -> dict:
    async with _lock:
        import time
        now = time.time()
        if not force and _state["checked_at"] and now - _state["checked_at"] < 300:
            return dict(_state)
        settings = SettingsManager.current()
        repo_url = settings.upstream_repo or "https://github.com/weebzone/Telegram-Stremio"
        branch = settings.upstream_branch or "master"
        parsed = _parse_repo(repo_url)
        if not parsed:
            _state["error"] = "invalid upstream_repo"
            _state["checked_at"] = now
            return dict(_state)
        owner, repo = parsed
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/Backend/__init__.py"
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                r = await client.get(raw_url)
                r.raise_for_status()
                m = _VERSION_RE.search(r.text)
                if not m:
                    _state["error"] = "version not found in upstream"
                    _state["checked_at"] = now
                    return dict(_state)
                remote = m.group(1).strip()
                _state["latest"] = remote
                _state["update_available"] = _cmp_version(__version__, remote) < 0
                _state["error"] = ""
                _state["checked_at"] = now
                if _state["update_available"]:
                    LOGGER.info(f"New version available: {remote} (current {__version__})")
                else:
                    LOGGER.debug(f"Version check: up to date ({__version__})")
        except Exception as e:
            _state["error"] = str(e)
            _state["checked_at"] = now
            LOGGER.warning(f"Version check failed: {e}")
        return dict(_state)


def get_version_status() -> dict:
    return {
        "current": __version__,
        "latest": _state["latest"],
        "update_available": bool(_state["update_available"]),
        "error": _state["error"],
        "checked_at": _state["checked_at"],
    }


async def version_check_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await check_upstream_version(force=True)
        except Exception as e:
            LOGGER.error(f"version_check_loop error: {e}")
        await asyncio.sleep(_CHECK_INTERVAL)
