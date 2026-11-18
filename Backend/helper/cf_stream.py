import asyncio
import hashlib
import hmac
import time

import httpx

from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER

#----- Signed Cloudflare links stay valid this long. The Worker can't read the token DB, so a
#----- revoked or expired token keeps streaming on Cloudflare until its links run out.
LINK_TTL = 48 * 3600

#----- Max clock skew accepted on requests signed by the Worker
REQUEST_MAX_AGE = 300


def cf_enabled() -> bool:
    s = SettingsManager.current()
    return s.cf_stream_mode != "off" and bool(s.cf_stream_url and s.cf_stream_secret)


def _hmac(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


#----- Cloudflare twin of a /dl/{token}/{id}/{name} link. Only /dl/{token}/{id} and the expiry
#----- are signed, so players re-encoding the file name don't break the signature.
def cf_stream_url(token: str, file_id: str, name: str, url: str = None, secret: str = None) -> str:
    s = SettingsManager.current()
    exp = int(time.time()) + LINK_TTL
    sig = _hmac(secret or s.cf_stream_secret, f"/dl/{token}/{file_id}:{exp}")[:32]
    return f"{url or s.cf_stream_url}/dl/{token}/{file_id}/{name}?e={exp}&s={sig}"


#----- Check a Worker -> app request signed as HMAC(secret, "{ts}\n{METHOD} {path}\n{body}")
def verify_worker_request(method: str, path: str, body: bytes, ts: str, sig: str, secret: str = None) -> bool:
    secret = secret or SettingsManager.current().cf_stream_secret
    if not secret or not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > REQUEST_MAX_AGE:
            return False
    except ValueError:
        return False
    expected = _hmac(secret, f"{ts}\n{method} {path}\n{body.decode('utf-8', 'replace')}")
    return hmac.compare_digest(expected, sig)


#----- Ask the Worker to reload bot tokens / the userbot session now instead of within 10 minutes
async def notify_worker(url: str = None, secret: str = None) -> None:
    if not url and not cf_enabled():
        return
    s = SettingsManager.current()
    url, secret = url or s.cf_stream_url, secret or s.cf_stream_secret
    ts, body = str(int(time.time())), "{}"
    headers = {"x-cf-time": ts, "x-cf-sig": _hmac(secret, f"{ts}\nPOST /api/sync\n{body}"), "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{url}/api/sync", content=body, headers=headers)
        if r.status_code != 200:
            LOGGER.warning(f"[CF] Worker sync failed: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        LOGGER.warning(f"[CF] Worker sync failed: {e}")


#----- Fire-and-forget: never delays the save or login that triggered it
def sync_worker_soon(url: str = None, secret: str = None) -> None:
    asyncio.create_task(notify_worker(url, secret))
