"""
WebDAV endpoint for Telegram-Stremio.

URL:  /webdav/{token}/...

Auth:
  - Path token = valid API token (same as Stremio)
  - Optional HTTP Basic: WEBDAV_USER / WEBDAV_PASSWORD from config.env

Methods: OPTIONS, PROPFIND, HEAD, GET  (read-only)

Supports the same media as Stremio streams:
  - single files
  - multi-quality (each quality is a separate file in the folder)
  - split parts (.001/.002) joined as one virtual file
  - split zip archives (via existing zip streamer)
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import List
from urllib.parse import quote, unquote
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, HTTPException, Request, Response

from Backend.config import Telegram
from Backend.fastapi.routes import stream_routes as sr
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.encrypt import decode_string
from Backend.helper.settings_manager import SettingsManager
from Backend.helper.webdav_fs import VNode, fs, normalize_path
from Backend.logger import LOGGER

router = APIRouter(tags=["WebDAV"])

DAV_NS = "DAV:"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _webdav_credentials():
    """Prefer live Settings page values; fall back to config.env."""
    try:
        s = SettingsManager.current()
        user = (getattr(s, "webdav_user", None) or "").strip()
        password = getattr(s, "webdav_password", None) or ""
        if user or password:
            return user, password
    except Exception:
        pass
    return (getattr(Telegram, "WEBDAV_USER", "") or "", getattr(Telegram, "WEBDAV_PASSWORD", "") or "")


def _basic_ok(request: Request) -> bool:
    user, password = _webdav_credentials()
    if not user and not password:
        return True  # Basic auth disabled
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        u, _, p = decoded.partition(":")
        return u == user and p == password
    except Exception:
        return False


async def _require_webdav_auth(request: Request, token: str) -> dict:
    if not _basic_ok(request):
        raise HTTPException(
            status_code=401,
            detail="WebDAV Basic auth required. Set username/password in Settings → WebDAV, or clear both fields to disable Basic auth.",
            headers={"WWW-Authenticate": 'Basic realm="Telegram-Stremio WebDAV"'},
        )
    try:
        token_data = await verify_token(token)
    except HTTPException as e:
        # Re-raise as 401 JSON-friendly (do not use admin login redirect)
        raise HTTPException(status_code=401, detail=f"Invalid API token for WebDAV: {e.detail}")
    if token_data.get("subscription_expired") or token_data.get("limit_exceeded"):
        raise HTTPException(status_code=403, detail="Token expired or limit exceeded")
    return token_data


# ---------------------------------------------------------------------------
# PROPFIND helpers
# ---------------------------------------------------------------------------

def _href(token: str, path: str, is_dir: bool) -> str:
    path = normalize_path(path)
    segments = [quote(s, safe="") for s in path.strip("/").split("/") if s]
    base = f"/webdav/{token}/" + "/".join(segments)
    if is_dir and not base.endswith("/"):
        base += "/"
    return base


def _http_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _iso_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prop_response(token: str, node: VNode) -> Element:
    resp = Element(f"{{{DAV_NS}}}response")
    href = SubElement(resp, f"{{{DAV_NS}}}href")
    href.text = _href(token, node.path, node.is_dir)

    propstat = SubElement(resp, f"{{{DAV_NS}}}propstat")
    prop = SubElement(propstat, f"{{{DAV_NS}}}prop")

    SubElement(prop, f"{{{DAV_NS}}}displayname").text = node.name or "root"
    SubElement(prop, f"{{{DAV_NS}}}getlastmodified").text = _http_date(node.mtime)
    SubElement(prop, f"{{{DAV_NS}}}getetag").text = (
        f'"{hash((node.path, node.size, int(node.mtime))) & 0xFFFFFFFF:x}"'
    )
    SubElement(prop, f"{{{DAV_NS}}}creationdate").text = _iso_date(node.mtime)

    if node.is_dir:
        rt = SubElement(prop, f"{{{DAV_NS}}}resourcetype")
        SubElement(rt, f"{{{DAV_NS}}}collection")
        SubElement(prop, f"{{{DAV_NS}}}getcontentlength").text = "0"
    else:
        SubElement(prop, f"{{{DAV_NS}}}resourcetype")
        SubElement(prop, f"{{{DAV_NS}}}getcontentlength").text = str(max(0, node.size))
        SubElement(prop, f"{{{DAV_NS}}}getcontenttype").text = node.content_type

    SubElement(propstat, f"{{{DAV_NS}}}status").text = "HTTP/1.1 200 OK"
    return resp


def _multistatus_xml(token: str, nodes: List[VNode]) -> bytes:
    root = Element(f"{{{DAV_NS}}}multistatus")
    for node in nodes:
        root.append(_prop_response(token, node))
    return tostring(root, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Video streaming — reuses Stremio stream_routes (single / split / zip)
# ---------------------------------------------------------------------------

async def _stream_video(request: Request, node: VNode, token: str, token_data: dict):
    if not node.stream_id:
        raise HTTPException(status_code=404, detail="No stream id for this file")

    try:
        decoded = await decode_string(node.stream_id)
    except Exception as e:
        LOGGER.warning("[WebDAV] decode stream id failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid stream id")

    sid = node.stream_id

    # Same dispatch order as stream_routes.stream_handler
    if decoded.get("global"):
        if decoded.get("zip"):
            return await sr.global_zip_media_streamer(
                request=request,
                parts_payload=decoded["parts"],
                token=token,
                token_data=token_data,
                stream_id_hash=sid,
            )
        if "parts" in decoded:
            return await sr.global_virtual_media_streamer(
                request=request,
                parts_payload=decoded["parts"],
                token=token,
                token_data=token_data,
                stream_id_hash=sid,
            )
        return await sr.global_media_streamer(
            request=request,
            chat_id=int(decoded["chat_id"]),
            msg_id=int(decoded["msg_id"]),
            token=token,
            token_data=token_data,
            stream_id_hash=sid,
        )

    if "parts" in decoded:
        if decoded.get("zip"):
            return await sr.db_zip_media_streamer(
                request=request,
                parts_payload=decoded["parts"],
                token=token,
                token_data=token_data,
                stream_id_hash=sid,
            )
        return await sr.virtual_media_streamer(
            request=request,
            parts_payload=decoded["parts"],
            token=token,
            token_data=token_data,
            stream_id_hash=sid,
        )

    msg_id = decoded.get("msg_id")
    if not msg_id:
        raise HTTPException(status_code=400, detail="Missing msg_id in stream id")
    chat_id = int(f"-100{decoded['chat_id']}")
    return await sr.media_streamer(
        request=request,
        chat_id=chat_id,
        msg_id=int(msg_id),
        token=token,
        token_data=token_data,
        stream_id_hash=sid,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.options("/webdav/{token}")
@router.options("/webdav/{token}/{path:path}")
async def webdav_options(token: str, path: str = ""):
    return Response(
        status_code=200,
        headers={
            "Allow": "OPTIONS, GET, HEAD, PROPFIND",
            "DAV": "1, 2",
            "MS-Author-Via": "DAV",
            "Accept-Ranges": "bytes",
        },
    )


@router.api_route("/webdav/{token}", methods=["PROPFIND"])
@router.api_route("/webdav/{token}/{path:path}", methods=["PROPFIND"])
async def webdav_propfind(request: Request, token: str, path: str = ""):
    await _require_webdav_auth(request, token)
    depth = request.headers.get("Depth", "1")
    vpath = normalize_path("/" + unquote(path or ""))
    node = await fs.resolve(vpath)
    if node is None:
        raise HTTPException(status_code=404, detail="Not found")

    nodes: List[VNode] = [node]
    if node.is_dir and depth != "0":
        nodes.extend(await fs.list_dir(vpath))

    return Response(
        content=_multistatus_xml(token, nodes),
        status_code=207,
        media_type="application/xml; charset=utf-8",
        headers={"DAV": "1, 2"},
    )


@router.head("/webdav/{token}")
@router.head("/webdav/{token}/{path:path}")
@router.get("/webdav/{token}")
@router.get("/webdav/{token}/{path:path}")
async def webdav_get(request: Request, token: str, path: str = ""):
    token_data = await _require_webdav_auth(request, token)
    vpath = normalize_path("/" + unquote(path or ""))
    node = await fs.resolve(vpath)
    if node is None:
        raise HTTPException(status_code=404, detail="Not found")

    if node.is_dir:
        children = await fs.list_dir(vpath)
        rows = []
        parent = normalize_path("/".join(vpath.rstrip("/").split("/")[:-1]) or "/")
        if vpath not in ("", "/"):
            rows.append(f'<li><a href="{_href(token, parent, True)}">../</a></li>')
        for c in sorted(children, key=lambda n: (not n.is_dir, n.name.lower())):
            href = _href(token, c.path, c.is_dir)
            label = c.name + ("/" if c.is_dir else "")
            size = "" if c.is_dir else f" ({c.size} bytes)"
            rows.append(f'<li><a href="{href}">{label}</a>{size}</li>')
        body = (
            f"<!DOCTYPE html><html><head><meta charset=utf-8><title>Index of {vpath}</title></head>"
            f"<body><h1>Index of {vpath}</h1><ul>{''.join(rows)}</ul>"
            f"<p><em>Telegram-Stremio WebDAV</em></p></body></html>"
        ).encode("utf-8")
        return Response(content=body, media_type="text/html; charset=utf-8")

    if node.nfo_body is not None:
        headers = {
            "Content-Type": node.content_type,
            "Content-Length": str(len(node.nfo_body)),
            "Accept-Ranges": "bytes",
        }
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return Response(content=node.nfo_body, media_type=node.content_type, headers=headers)

    if node.kind in ("movie_video", "episode_video"):
        if request.method == "HEAD":
            return Response(
                status_code=200,
                headers={
                    "Content-Type": node.content_type,
                    "Content-Length": str(max(1, node.size)),
                    "Accept-Ranges": "bytes",
                },
            )
        return await _stream_video(request, node, token, token_data)

    raise HTTPException(status_code=404, detail="Unsupported node type")


@router.api_route(
    "/webdav/{token}",
    methods=["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"],
)
@router.api_route(
    "/webdav/{token}/{path:path}",
    methods=["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"],
)
async def webdav_readonly(token: str, path: str = ""):
    return Response(status_code=403, content="WebDAV is read-only")


@router.post("/webdav/{token}/refresh")
async def webdav_refresh(request: Request, token: str):
    await _require_webdav_auth(request, token)
    fs.invalidate()
    await fs.ensure_tree()
    return {"status": "ok", "message": "WebDAV filesystem cache refreshed"}
