"""Send a freshly created Request-page submission to an external HTTP API
(e.g. an n8n webhook) for downstream processing, in addition to the Telegram
notifier. Mirrors the fire-and-forget pattern of request_notifier.py.

Configured via two DB-backed settings (no hardcoding):
  - external_api_url:    full webhook URL
  - external_api_token:  Bearer token sent as `Authorization: Bearer <token>`

If either is empty the call is skipped silently (the request still succeeds
and the Telegram notifier still fires). Failures are logged, never raised, so
a broken external API can never break a user's request.
"""
from asyncio import create_task

import requests

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

_TIMEOUT = 10


def _build_payload(doc: dict) -> dict:
    is_tv = doc.get("media_type") == "tv"
    seasons = [s for s in (doc.get("season_numbers") or []) if s]
    return {
        "imdb": doc.get("imdb_id") or "",
        "nombre": (doc.get("title") or "").strip(),
        "tipo": "series" if is_tv else "movies",
        # Película -> 0; Serie -> mayor temporada pedida (0 si no se eligió ninguna)
        "temporada": max(seasons) if (is_tv and seasons) else 0,
        # Episodio específico (solo desde Stremio stream prompt; 0 = toda la temporada o movie)
        "episodio": (doc.get("episode_num") or 0),
    }


async def _notify(doc: dict) -> None:
    settings = SettingsManager.current()
    url = settings.external_api_url
    token = settings.external_api_token
    if not url or not token:
        return

    payload = _build_payload(doc)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            LOGGER.error(
                f"External API notify failed for '{doc.get('title')}': "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        else:
            LOGGER.info(
                f"External API notify OK for '{doc.get('title')}': HTTP {resp.status_code}"
            )
    except Exception as e:
        LOGGER.error(f"External API notify error for '{doc.get('title')}': {e}")


#----- Fire-and-forget notification for a freshly created request (call once per new title)
def notify_external_api(doc: dict) -> None:
    try:
        create_task(_notify(dict(doc)))
    except RuntimeError:
        LOGGER.warning("notify_external_api called outside an event loop; skipped")
