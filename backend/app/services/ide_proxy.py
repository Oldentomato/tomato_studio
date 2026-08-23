from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from starlette.responses import Response, StreamingResponse

from ..models import Workspace
from . import docker_ws
from .specs import uses_subpath_proxy

_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_HTML_ATTR = re.compile(
    r"""((?:href|src|action|data-base-url|data-baseurl|poster)\s*=\s*["'])/(?!/|api/workspaces/)""",
    re.I,
)
_HTML_HEAD = re.compile(r"(<head[^>]*>)", re.I)
_COOKIE_DOMAIN = re.compile(r";\s*Domain=[^;]*", re.I)
_COOKIE_PATH = re.compile(r";\s*Path=([^;]*)", re.I)
_FRAME_ANCESTORS = re.compile(r"frame-ancestors[^;]*;?", re.I)

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
    follow_redirects=False,
)

_MAX_HTML_REWRITE = 5 * 1024 * 1024


def _upstream(workspace: Workspace) -> tuple[str, int]:
    docker_ws.sync_from_docker(workspace)
    port = workspace.host_port
    if workspace.status != "running" or not port:
        raise HTTPException(status_code=503, detail="화면이 실행 중이 아닙니다.")
    return docker_ws.resolved_public_workspace_host(), port


def _prefix(workspace_id: str, mode: str = "ide") -> str:
    suffix = "ui" if mode == "web" else "ide"
    return f"/api/workspaces/{workspace_id}/{suffix}"


def _uses_subpath(workspace: Workspace, mode: str) -> bool:
    return mode == "web" and uses_subpath_proxy(workspace.docker_image or "")


def _public_host(headers, fallback: str = "") -> str:
    return (headers.get("host") or fallback).strip()


def _join_upstream(host: str, port: int, path: str, query: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    url = f"http://{host}:{port}{path}"
    if query:
        url = f"{url}?{query}"
    return url


def _target_url(
    host: str,
    port: int,
    path: str,
    query: str,
    *,
    prefix: str,
    full_prefix: bool,
) -> str:
    rel = path.lstrip("/")
    if full_prefix:
        base = prefix.rstrip("/")
        forwarded = f"{base}/{rel}" if rel else f"{base}/"
        return _join_upstream(host, port, forwarded, query)
    return _join_upstream(host, port, f"/{rel}" if rel else "/", query)


def _filter_request_headers(
    headers: dict[str, str],
    request: Request,
    prefix: str,
    mode: str,
) -> dict[str, str]:
    out = {k: v for k, v in headers.items() if k.lower() not in _HOP}
    public_host = _public_host(request.headers, request.url.netloc)
    # Keep the browser Host so CSRF Origin checks match. TCP still goes to the container.
    out["host"] = public_host
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    out["x-forwarded-proto"] = proto
    out["x-forwarded-host"] = public_host
    out["x-forwarded-for"] = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
    if mode == "web":
        out["x-forwarded-prefix"] = prefix
        out["x-script-name"] = prefix
        out["x-forwarded-path"] = prefix
    return out


def _path_from_location(value: str, origin: str) -> str:
    if value.startswith(origin):
        rest = value[len(origin) :] or "/"
        if not rest.startswith("/"):
            rest = f"/{rest}"
        return rest
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        rest = parsed.path or "/"
        if parsed.query:
            rest = f"{rest}?{parsed.query}"
        return rest
    return value


def _rewrite_location(value: str, prefix: str, origin: str) -> str:
    prefix_n = prefix.rstrip("/")
    path = _path_from_location(value, origin)
    if path == prefix_n or path.startswith(f"{prefix_n}/") or path.startswith(f"{prefix_n}?"):
        return path
    if path.startswith("/"):
        return f"{prefix_n}{path}"
    return value


def _same_proxy_path(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


def _rewrite_cookie(value: str, prefix: str) -> str:
    cookie = _COOKIE_DOMAIN.sub("", value)
    prefix_path = prefix.rstrip("/") + "/"
    match = _COOKIE_PATH.search(cookie)
    if match:
        current = (match.group(1) or "").strip()
        if current.rstrip("/") == prefix.rstrip("/") or current.startswith(prefix.rstrip("/")):
            return cookie
        return _COOKIE_PATH.sub(f"; Path={prefix_path}", cookie, count=1)
    return f"{cookie}; Path={prefix_path}"


def _rewrite_html(html: str, prefix: str) -> str:
    base = prefix.rstrip("/") + "/"
    if _HTML_HEAD.search(html) and "<base " not in html.lower():
        html = _HTML_HEAD.sub(rf'\1<base href="{base}">', html, count=1)
    html = _HTML_ATTR.sub(lambda match: f"{match.group(1)}{base}", html)
    return html


def _response_headers(
    response: httpx.Response,
    prefix: str,
    origin: str,
    *,
    mode: str,
    drop_encoding: bool = False,
) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for key, value in response.headers.multi_items():
        lower = key.lower()
        if lower in _HOP or lower == "content-length":
            continue
        if drop_encoding and lower in {"content-encoding", "content-md5"}:
            continue
        if lower == "location":
            headers.append((key, _rewrite_location(value, prefix, origin)))
            continue
        if lower == "set-cookie":
            headers.append((key, _rewrite_cookie(value, prefix)))
            continue
        if mode == "web" and lower == "x-frame-options":
            continue
        if mode == "web" and lower == "content-security-policy":
            value = _FRAME_ANCESTORS.sub("frame-ancestors *;", value)
            headers.append((key, value))
            continue
        headers.append((key, value))
    return headers


async def proxy_http(
    request: Request,
    db: Session,
    workspace_id: str,
    path: str = "",
    mode: str = "ide",
) -> Response:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    host, port = _upstream(workspace)
    origin = f"http://{host}:{port}"
    prefix = _prefix(workspace_id, mode)
    full_prefix = _uses_subpath(workspace, mode)
    url = _target_url(
        host,
        port,
        path,
        request.url.query,
        prefix=prefix,
        full_prefix=full_prefix,
    )
    headers = _filter_request_headers(dict(request.headers), request, prefix, mode)
    body = await request.body()
    req = _client.build_request(request.method, url, headers=headers, content=body or None)
    req.headers["host"] = headers["host"]
    upstream = await _client.send(req, stream=True)
    content_type = (upstream.headers.get("content-type") or "").lower()
    rewrite_html = mode == "web" and not full_prefix and "text/html" in content_type

    if rewrite_html:
        raw = await upstream.aread()
        await upstream.aclose()
        if len(raw) <= _MAX_HTML_REWRITE:
            charset = "utf-8"
            match = re.search(r"charset=([\w-]+)", content_type)
            if match:
                charset = match.group(1)
            try:
                text = raw.decode(charset)
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            payload = _rewrite_html(text, prefix).encode(charset, errors="replace")
        else:
            payload = raw
        out_headers = _response_headers(upstream, prefix, origin, mode=mode, drop_encoding=True)
        response = Response(content=payload, status_code=upstream.status_code)
        for key, value in out_headers:
            if key.lower() == "location" and _same_proxy_path(value, request.url.path):
                continue
            response.headers.append(key, value)
        return response

    out_headers = _response_headers(upstream, prefix, origin, mode=mode)

    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    response = StreamingResponse(stream(), status_code=upstream.status_code)
    for key, value in out_headers:
        if key.lower() == "location" and _same_proxy_path(value, request.url.path):
            continue
        response.headers.append(key, value)
    return response


async def proxy_ws(
    websocket: WebSocket,
    db: Session,
    workspace_id: str,
    path: str = "",
    mode: str = "ide",
) -> None:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        await websocket.close(code=4404)
        return
    try:
        host, port = _upstream(workspace)
    except HTTPException:
        await websocket.close(code=4403)
        return

    query = websocket.url.query
    prefix = _prefix(workspace_id, mode)
    full_prefix = _uses_subpath(workspace, mode)
    rel = path.lstrip("/")
    if full_prefix:
        base = prefix.rstrip("/")
        forwarded = f"{base}/{rel}" if rel else f"{base}/"
        url = f"ws://{host}:{port}{forwarded}"
    else:
        url = f"ws://{host}:{port}/{rel}"
    if query:
        url = f"{url}?{query}"

    headers = {
        k: v
        for k, v in websocket.headers.items()
        if k.lower()
        not in {
            "host",
            "connection",
            "upgrade",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
            "content-length",
        }
    }
    public_host = _public_host(websocket.headers, websocket.url.netloc)
    headers["host"] = public_host
    proto = websocket.headers.get("x-forwarded-proto") or websocket.url.scheme
    if proto == "ws":
        proto = "http"
    elif proto == "wss":
        proto = "https"
    headers["x-forwarded-proto"] = proto
    headers["x-forwarded-host"] = public_host
    if mode == "web":
        headers["x-forwarded-prefix"] = prefix
        headers["x-script-name"] = prefix
    subprotocol = websocket.headers.get("sec-websocket-protocol")

    try:
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:
        from websockets import connect as ws_connect

    kwargs = {"open_timeout": 15}
    if subprotocol:
        kwargs["subprotocols"] = [item.strip() for item in subprotocol.split(",") if item.strip()]
    try:
        upstream_ctx = ws_connect(url, additional_headers=headers, **kwargs)
    except TypeError:
        upstream_ctx = ws_connect(url, extra_headers=headers, **kwargs)

    try:
        upstream = await upstream_ctx.__aenter__()
    except Exception:
        await websocket.close(code=1011)
        return

    try:
        proto = None
        if hasattr(upstream, "subprotocol"):
            proto = upstream.subprotocol
        await websocket.accept(subprotocol=proto)

        async def to_upstream() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    data = message.get("bytes")
                    if data is not None:
                        await upstream.send(data)
                        continue
                    text = message.get("text")
                    if text is not None:
                        await upstream.send(text)
            except WebSocketDisconnect:
                pass

        async def to_client() -> None:
            try:
                async for payload in upstream:
                    if isinstance(payload, bytes):
                        await websocket.send_bytes(payload)
                    else:
                        await websocket.send_text(str(payload))
            except Exception:
                pass

        tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    finally:
        with suppress(Exception):
            await upstream_ctx.__aexit__(None, None, None)
        with suppress(Exception):
            await websocket.close()
