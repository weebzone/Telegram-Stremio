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

#----- Deduplicate webhook calls: Stremio retries the stream GET (fetch/retry),
# so the same (imdb, season, episode) can hit this code path ~7x in a few seconds.
# This cache ensures we only POST to the webhook ONCE per unique request per window.
import time as _time
_RECENT_NOTIFY = {}   # {(imdb, season_str, ep): timestamp}
_NOTIFY_TTL = 15  # seconds — Stremio retries stream GET up to ~10s; 15s TTL ensures
                  # BOTH the retry + meta-refresh stay deduplicated (was 5s → 2x fire)


def _notify_key(doc: dict) -> tuple:
    seasons = [s for s in (doc.get("season_numbers") or []) if s]
    season_str = ",".join(str(s) for s in sorted(seasons)) or "none"
    return (doc.get("imdb_id") or "", season_str, doc.get("episode_num") or 0)


def _is_recently_sent(key: tuple) -> bool:
    now = _time.monotonic()
    last = _RECENT_NOTIFY.get(key)
    if last is not None and (now - last) < _NOTIFY_TTL:
        return True
    _RECENT_NOTIFY[key] = now
    # prune old entries (keeps the dict tiny)
    for k, ts in list(_RECENT_NOTIFY.items()):
        if (now - ts) >= _NOTIFY_TTL * 3:
            del _RECENT_NOTIFY[k]
    return False


def _build_payload(doc: dict) -> dict:
    # media_type in DB may be "tv" or "series" (raw Cinemeta); normalize to "tv"
    raw_type = doc.get("media_type") or ""
    is_tv = raw_type in ("tv", "series")
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
    #----- Dedupe: skip if we already fired this exact (imdb, season, episode) recently
    key = _notify_key(doc)
    if _is_recently_sent(key):
        LOGGER.info(f"External API notify SKIPPED (duplicate): '{doc.get('title')}' imdb={key[0]} s={key[1]} e={key[2]}")
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
