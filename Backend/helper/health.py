import asyncio
from datetime import datetime

import httpx

from Backend import db
from Backend.helper.metadata import get_tmdb_client, tmdb_api_key
from Backend.helper.settings_manager import SettingsManager
from Backend.pyrofork.bot import (
    StreamBot,
    client_dc_map,
    client_failures,
    multi_clients,
    work_loads,
)

#----- Rough Atlas free-tier (M0) storage ceiling, used only as a usage guide
_FREE_TIER_BYTES = 512 * 1024 * 1024


async def _check_databases() -> dict:
    items = []
    for key, mdb in db.dbs.items():
        entry = {"name": key, "status": "down", "size_mb": 0, "usage_pct": 0, "message": ""}
        try:
            await asyncio.wait_for(mdb.command("ping"), timeout=6)
            entry["status"] = "ok"
            try:
                stats = await asyncio.wait_for(mdb.command("dbstats"), timeout=6)
                size = stats.get("storageSize", 0) or 0
                entry["size_mb"] = round(size / (1024 * 1024), 1)
                entry["usage_pct"] = min(100, round(size / _FREE_TIER_BYTES * 100, 1))
            except Exception:
                pass
        except Exception as e:
            entry["message"] = str(e)[:140]
        items.append(entry)

    up = sum(1 for i in items if i["status"] == "ok")
    status = "ok" if items and up == len(items) else ("degraded" if up else "down")
    return {"key": "databases", "label": "Databases", "status": status, "up": up, "total": len(items), "items": items}


def _check_bots() -> dict:
    clients = []
    for idx in sorted(multi_clients.keys()):
        client = multi_clients[idx]
        clients.append({
            "name": "Userbot" if idx < 0 else f"Bot {idx + 1}",
            "connected": bool(getattr(client, "is_connected", False)),
            "dc": client_dc_map.get(idx),
            "workload": work_loads.get(idx, 0),
            "failures": client_failures.get(idx, 0),
        })
    up = sum(1 for c in clients if c["connected"])
    status = "ok" if clients and up == len(clients) else ("degraded" if up else "down")
    return {
        "key": "bots", "label": "Telegram Clients", "status": status,
        "up": up, "total": len(clients), "items": clients,
        "primary_connected": bool(getattr(StreamBot, "is_connected", False)),
    }


async def _check_tmdb() -> dict:
    if not tmdb_api_key():
        return {"key": "tmdb", "label": "TMDB API", "status": "not_configured",
                "message": "No TMDB API key set — metadata matching will be limited."}
    try:
        movie = await asyncio.wait_for(get_tmdb_client().movie(550).details(), timeout=10)
        if getattr(movie, "title", None):
            return {"key": "tmdb", "label": "TMDB API", "status": "ok", "message": "API key is valid."}
        return {"key": "tmdb", "label": "TMDB API", "status": "error", "message": "Unexpected TMDB response."}
    except Exception as e:
        return {"key": "tmdb", "label": "TMDB API", "status": "error", "message": str(e)[:140]}


async def _check_base_url() -> dict:
    base = SettingsManager.current().base_url
    if not base:
        return {"key": "base_url", "label": "Base URL", "status": "not_configured",
                "message": "Base URL not set — Stremio can't reach your streams."}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(base)
        return {"key": "base_url", "label": "Base URL", "status": "ok",
                "message": f"Reachable (HTTP {resp.status_code}).", "url": base}
    except Exception as e:
        return {"key": "base_url", "label": "Base URL", "status": "error", "message": str(e)[:140], "url": base}


async def run_health_checks() -> dict:
    databases = await _check_databases()
    bots = _check_bots()
    tmdb = await _check_tmdb()
    base_url = await _check_base_url()

    statuses = [databases["status"], bots["status"], tmdb["status"], base_url["status"]]
    if "down" in (databases["status"], bots["status"]):
        overall = "critical"
    elif any(s in ("down", "degraded", "error", "not_configured") for s in statuses):
        overall = "warning"
    else:
        overall = "ok"

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "overall": overall,
        "sections": [databases, bots, tmdb, base_url],
    }
