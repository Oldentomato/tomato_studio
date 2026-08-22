from __future__ import annotations

import asyncio
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from starlette.responses import Response, StreamingResponse

from ..models import Workspace
from . import docker_ws

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

_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
    follow_redirects=False,
)


def _upstream(workspace: Workspace) -> tuple[str, int]:
    docker_ws.sync_from_docker(workspace)
    port = workspace.host_port
    if workspace.status != "running" or not port:
        raise HTTPException(status_code=503, detail="code-server가 실행 중이 아닙니다.")
    return docker_ws.resolved_public_workspace_host(), port


def _prefix(workspace_id: str) -> str:
    return f"/api/workspaces/{workspace_id}/ide"


def _target_url(host: str, port: int, path: str, query: str) -> str:
    rel = path.lstrip("/")
    url = f"http://{host}:{port}/{rel}"
    if query:
        url = f"{url}?{query}"
    return url


def _filter_request_headers(headers: dict[str, str], host: str, port: int, request: Request) -> dict[str, str]:
    out = {k: v for k, v in headers.items() if k.lower() not in _HOP}
    out["host"] = f"{host}:{port}"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    out["x-forwarded-proto"] = proto
    out["x-forwarded-host"] = request.headers.get("host") or request.url.hostname or ""
    out["x-forwarded-for"] = request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
    return out


def _rewrite_location(value: str, workspace_id: str, origin: str) -> str:
    prefix = _prefix(workspace_id)
    if value.startswith(origin):
        rest = value[len(origin) :]
        if not rest.startswith("/"):
            rest = f"/{rest}"
        return f"{prefix}{rest}"
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        rest = parsed.path or "/"
        if parsed.query:
            rest = f"{rest}?{parsed.query}"
        return f"{prefix}{rest}"
    if value.startswith("/") and not value.startswith(prefix):
        return f"{prefix}{value}"
    return value


def _response_headers(response: httpx.Response, workspace_id: str, origin: str) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    prefix = _prefix(workspace_id)
    for key, value in response.headers.multi_items():
        lower = key.lower()
        if lower in _HOP or lower == "content-length":
            continue
        if lower == "location":
            headers.append((key, _rewrite_location(value, workspace_id, origin)))
            continue
        if lower == "set-cookie":
            headers.append((key, value.replace("Path=/", f"Path={prefix}/", 1)))
            continue
        headers.append((key, value))
    return headers


async def proxy_http(request: Request, db: Session, workspace_id: str, path: str = "") -> Response:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    host, port = _upstream(workspace)
    origin = f"http://{host}:{port}"
    url = _target_url(host, port, path, request.url.query)
    headers = _filter_request_headers(dict(request.headers), host, port, request)
    body = await request.body()
    req = _client.build_request(request.method, url, headers=headers, content=body or None)
    upstream = await _client.send(req, stream=True)
    out_headers = _response_headers(upstream, workspace_id, origin)

    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    response = StreamingResponse(stream(), status_code=upstream.status_code)
    for key, value in out_headers:
        response.headers.append(key, value)
    return response


async def proxy_ws(websocket: WebSocket, db: Session, workspace_id: str, path: str = "") -> None:
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
    rel = path.lstrip("/")
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
    headers["host"] = f"{host}:{port}"
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
