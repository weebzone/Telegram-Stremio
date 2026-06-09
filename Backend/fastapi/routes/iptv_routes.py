import ipaddress
import re
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from Backend import db
from Backend.config import Telegram
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.iptv import (
    get_iptv_channel_by_stream,
    sign_proxy_target,
    verify_proxy_target,
)


router = APIRouter(prefix="/iptv", tags=["IPTV Streaming"])

HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}
PASSTHROUGH_HEADERS = {
    "accept-ranges",
    "cache-control",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "expires",
    "last-modified",
}
URI_ATTRIBUTE_PATTERN = re.compile(r'URI=(["\'])(.+?)\1')


def _ensure_proxy_access(token_data: dict) -> None:
    if token_data.get("subscription_expired"):
        raise HTTPException(status_code=403, detail="Subscription expired")
    if token_data.get("limit_exceeded"):
        raise HTTPException(status_code=429, detail="Streaming limit reached")


def _safe_target_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Unsupported IPTV URL")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise HTTPException(status_code=400, detail="Invalid IPTV host")
    try:
        address = ipaddress.ip_address(hostname)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
        ):
            raise HTTPException(status_code=400, detail="Invalid IPTV host")
    except ValueError:
        pass
    return url


def _source_from_channel(channel: dict, stream_id: str) -> dict:
    for source in channel.get("streams") or []:
        if str(source.get("id")) == str(stream_id):
            return source
    raise HTTPException(status_code=404, detail="IPTV stream not found")


def _request_headers(request: Request, source: dict) -> dict:
    headers = {
        "Accept": request.headers.get("accept", "*/*"),
        "Accept-Encoding": "identity",
    }
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    for name, value in (source.get("request_headers") or {}).items():
        if value:
            headers[str(name)] = str(value)
    if "User-Agent" not in headers:
        headers["User-Agent"] = request.headers.get(
            "user-agent",
            "Telegram-Stremio-IPTV/1.0",
        )
    return headers


def _response_headers(upstream: httpx.Response, *, rewritten: bool = False) -> dict:
    headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() in PASSTHROUGH_HEADERS
    }
    if rewritten:
        headers.pop("content-length", None)
        headers.pop("content-range", None)
        headers["Content-Type"] = "application/vnd.apple.mpegurl"
        headers["Cache-Control"] = "no-store"
    return headers


def _is_hls(url: str, response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    path = urlparse(url).path.lower()
    return content_type in HLS_CONTENT_TYPES or path.endswith(".m3u8")


def _rewrite_hls_manifest(content: str, base_url: str, token: str, stream_id: str) -> str:
    def proxied(value: str) -> str:
        target = _safe_target_url(urljoin(base_url, value.strip()))
        signed = sign_proxy_target(stream_id, target)
        return f"{Telegram.BASE_URL}/iptv/{token}/fetch/{signed}"

    output = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            output.append(line)
            continue
        if stripped.startswith("#"):
            def replace_uri(match):
                quote = match.group(1)
                return f"URI={quote}{proxied(match.group(2))}{quote}"

            output.append(URI_ATTRIBUTE_PATTERN.sub(replace_uri, line))
            continue
        output.append(proxied(stripped))
    return "\n".join(output) + ("\n" if content.endswith("\n") else "")


async def _proxy(
    request: Request,
    *,
    token: str,
    stream_id: str,
    target_url: str,
    source: dict,
):
    target_url = _safe_target_url(target_url)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(Telegram.IPTV_PROXY_TIMEOUT_SEC),
        follow_redirects=True,
    )
    method = request.method.upper()
    headers = _request_headers(request, source)

    try:
        upstream_request = client.build_request(method, target_url, headers=headers)
        upstream = await client.send(upstream_request, stream=True)
        if method == "HEAD" and upstream.status_code in {405, 501}:
            await upstream.aclose()
            headers["Range"] = "bytes=0-0"
            upstream_request = client.build_request("GET", target_url, headers=headers)
            upstream = await client.send(upstream_request, stream=True)

        if method == "HEAD":
            response_headers = _response_headers(upstream)
            status_code = upstream.status_code
            await upstream.aclose()
            await client.aclose()
            return Response(status_code=status_code, headers=response_headers)

        if _is_hls(str(upstream.url), upstream):
            raw = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            if len(raw) > 2 * 1024 * 1024:
                raise HTTPException(status_code=502, detail="IPTV playlist is unexpectedly large")
            encoding = upstream.encoding or "utf-8"
            manifest = raw.decode(encoding, errors="replace")
            rewritten = _rewrite_hls_manifest(
                manifest,
                str(upstream.url),
                token,
                stream_id,
            )
            return Response(
                content=rewritten,
                status_code=upstream.status_code,
                headers=_response_headers(upstream, rewritten=True),
                media_type="application/vnd.apple.mpegurl",
            )

        async def body_iterator():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            body_iterator(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
            media_type=upstream.headers.get("content-type"),
        )
    except HTTPException:
        await client.aclose()
        raise
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise HTTPException(status_code=504, detail="IPTV upstream timed out") from exc
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="IPTV upstream request failed") from exc


@router.api_route("/{token}/stream/{stream_id}", methods=["GET", "HEAD"])
async def proxy_iptv_stream(
    request: Request,
    token: str,
    stream_id: str,
    token_data: dict = Depends(verify_token),
):
    _ensure_proxy_access(token_data)
    channel = await get_iptv_channel_by_stream(db, stream_id)
    if not channel:
        raise HTTPException(status_code=404, detail="IPTV stream not found")
    source = _source_from_channel(channel, stream_id)
    return await _proxy(
        request,
        token=token,
        stream_id=stream_id,
        target_url=source.get("url"),
        source=source,
    )


@router.api_route("/{token}/fetch/{signed_target}", methods=["GET", "HEAD"])
async def proxy_iptv_child_resource(
    request: Request,
    token: str,
    signed_target: str,
    token_data: dict = Depends(verify_token),
):
    _ensure_proxy_access(token_data)
    try:
        payload = verify_proxy_target(signed_target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stream_id = str(payload["s"])
    channel = await get_iptv_channel_by_stream(db, stream_id)
    if not channel:
        raise HTTPException(status_code=404, detail="IPTV stream not found")
    source = _source_from_channel(channel, stream_id)
    return await _proxy(
        request,
        token=token,
        stream_id=stream_id,
        target_url=payload["u"],
        source=source,
    )
