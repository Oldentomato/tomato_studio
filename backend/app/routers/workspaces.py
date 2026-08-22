import asyncio
import json
from contextlib import suppress

import posixpath
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from ..db import SessionLocal, get_db
from ..models import Workspace
from ..schemas import FileListOut, WorkspaceCreate, WorkspaceOut
from ..services import docker_ws, ide_proxy, volume_files
from ..services.agent import workspace_out

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def _out(workspace: Workspace, *, sync: bool = True) -> WorkspaceOut:
    return workspace_out(workspace, sync=sync)


def _get(db: Session, workspace_id: str) -> Workspace:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="워크스페이스를 찾을 수 없습니다.")
    return workspace


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(db: Session = Depends(get_db)) -> list[WorkspaceOut]:
    items = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
    result = [_out(item, sync=item.status == "stopping") for item in items]
    db.commit()
    return result


@router.post("", response_model=WorkspaceOut)
def create_workspace(payload: WorkspaceCreate, db: Session = Depends(get_db)) -> WorkspaceOut:
    workspace = docker_ws.create_workspace(db, payload.name)
    return _out(workspace)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace_id: str, db: Session = Depends(get_db)) -> WorkspaceOut:
    workspace = _get(db, workspace_id)
    result = _out(workspace)
    db.commit()
    return result


@router.post("/{workspace_id}/start", response_model=WorkspaceOut)
def start_workspace(workspace_id: str, db: Session = Depends(get_db)) -> WorkspaceOut:
    workspace = _get(db, workspace_id)
    try:
        workspace = docker_ws.start_workspace(db, workspace)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _out(workspace)


@router.post("/{workspace_id}/stop", response_model=WorkspaceOut)
def stop_workspace(workspace_id: str, db: Session = Depends(get_db)) -> WorkspaceOut:
    workspace = _get(db, workspace_id)
    workspace = docker_ws.stop_workspace(db, workspace)
    return _out(workspace)


@router.post("/{workspace_id}/heartbeat", response_model=WorkspaceOut)
def heartbeat(workspace_id: str, db: Session = Depends(get_db)) -> WorkspaceOut:
    workspace = _get(db, workspace_id)
    workspace = docker_ws.touch_workspace(db, workspace)
    return _out(workspace)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str, db: Session = Depends(get_db)) -> None:
    workspace = _get(db, workspace_id)
    docker_ws.delete_workspace(db, workspace)


@router.get("/{workspace_id}/files", response_model=FileListOut)
def list_files(workspace_id: str, path: str = "", db: Session = Depends(get_db)) -> FileListOut:
    workspace = _get(db, workspace_id)
    try:
        rel = volume_files.safe_relpath(path)
        entries = volume_files.list_entries(rel, workspace=workspace)
    except volume_files.ContainerNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="경로를 찾을 수 없습니다.") from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=400, detail="폴더가 아닙니다.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FileListOut(path=rel, entries=entries)


@router.get("/{workspace_id}/files/content")
def download_workspace_file(workspace_id: str, path: str, db: Session = Depends(get_db)) -> Response:
    workspace = _get(db, workspace_id)
    try:
        filename, payload = volume_files.get_bytes(path, workspace=workspace)
    except volume_files.ContainerNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    quoted = quote(filename)
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{workspace_id}/files", response_model=FileListOut)
async def upload_workspace_files(
    workspace_id: str,
    path: str = "",
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
) -> FileListOut:
    workspace = _get(db, workspace_id)
    try:
        rel = volume_files.safe_relpath(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not files:
        raise HTTPException(status_code=400, detail="올릴 파일이 없습니다.")
    try:
        payload_items: list[tuple[str, bytes]] = []
        for item in files:
            name = volume_files.safe_filename(item.filename)
            payload = await item.read()
            dest = posixpath.join(rel, name) if rel else name
            payload_items.append((dest, payload))
        volume_files.put_files(payload_items, workspace=workspace)
        entries = volume_files.list_entries(rel, workspace=workspace)
    except volume_files.ContainerNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FileListOut(path=rel, entries=entries)


_IDE_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@router.api_route("/{workspace_id}/ide", methods=_IDE_METHODS, include_in_schema=False)
async def ide_root(request: Request, workspace_id: str, db: Session = Depends(get_db)):
    if request.method == "GET" and request.url.query == "":
        return RedirectResponse(url=f"/api/workspaces/{workspace_id}/ide/", status_code=307)
    return await ide_proxy.proxy_http(request, db, workspace_id, "")


@router.api_route("/{workspace_id}/ide/{path:path}", methods=_IDE_METHODS, include_in_schema=False)
async def ide_path(request: Request, workspace_id: str, path: str, db: Session = Depends(get_db)):
    return await ide_proxy.proxy_http(request, db, workspace_id, path)


@router.websocket("/{workspace_id}/ide")
async def ide_socket_root(websocket: WebSocket, workspace_id: str) -> None:
    db = SessionLocal()
    try:
        await ide_proxy.proxy_ws(websocket, db, workspace_id, "")
    finally:
        db.close()


@router.websocket("/{workspace_id}/ide/{path:path}")
async def ide_socket_path(websocket: WebSocket, workspace_id: str, path: str) -> None:
    db = SessionLocal()
    try:
        await ide_proxy.proxy_ws(websocket, db, workspace_id, path)
    finally:
        db.close()


@router.api_route("/{workspace_id}/ui", methods=_IDE_METHODS, include_in_schema=False)
async def ui_root(request: Request, workspace_id: str, db: Session = Depends(get_db)):
    if request.method == "GET" and request.url.query == "":
        return RedirectResponse(url=f"/api/workspaces/{workspace_id}/ui/", status_code=307)
    return await ide_proxy.proxy_http(request, db, workspace_id, "", mode="web")


@router.api_route("/{workspace_id}/ui/{path:path}", methods=_IDE_METHODS, include_in_schema=False)
async def ui_path(request: Request, workspace_id: str, path: str, db: Session = Depends(get_db)):
    return await ide_proxy.proxy_http(request, db, workspace_id, path, mode="web")


@router.websocket("/{workspace_id}/ui")
async def ui_socket_root(websocket: WebSocket, workspace_id: str) -> None:
    db = SessionLocal()
    try:
        await ide_proxy.proxy_ws(websocket, db, workspace_id, "", mode="web")
    finally:
        db.close()


@router.websocket("/{workspace_id}/ui/{path:path}")
async def ui_socket_path(websocket: WebSocket, workspace_id: str, path: str) -> None:
    db = SessionLocal()
    try:
        await ide_proxy.proxy_ws(websocket, db, workspace_id, path, mode="web")
    finally:
        db.close()


@router.websocket("/{workspace_id}/terminal")
async def container_terminal(websocket: WebSocket, workspace_id: str) -> None:
    await websocket.accept()
    db = SessionLocal()
    try:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            await websocket.send_text(
                json.dumps({"type": "error", "message": "워크스페이스를 찾을 수 없습니다."}, ensure_ascii=False)
            )
            await websocket.close(code=4404)
            return
        if (workspace.kind or "vscode") != "container":
            await websocket.send_text(
                json.dumps(
                    {"type": "error", "message": "일반 컨테이너에서만 터미널을 열 수 있습니다."},
                    ensure_ascii=False,
                )
            )
            await websocket.close(code=4400)
            return
        docker_ws.sync_from_docker(workspace)
        db.commit()
        if workspace.status != "running":
            await websocket.send_text(
                json.dumps({"type": "error", "message": "컨테이너가 실행 중이 아닙니다."}, ensure_ascii=False)
            )
            await websocket.close(code=4403)
            return
        docker_ws.touch_workspace(db, workspace)
        try:
            session = await asyncio.to_thread(docker_ws.open_container_exec, workspace)
        except Exception as exc:
            await websocket.send_text(
                json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
            )
            await websocket.close(code=1011)
            return
    finally:
        db.close()

    async def docker_to_ws() -> None:
        try:
            while True:
                chunk = await asyncio.to_thread(docker_ws.read_exec, session)
                if chunk is None:
                    break
                if chunk:
                    await websocket.send_bytes(chunk)
        except Exception:
            pass
        else:
            with suppress(Exception):
                await websocket.close()

    pump = asyncio.create_task(docker_to_ws())
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if raw:
                await asyncio.to_thread(docker_ws.write_exec, session, raw)
                continue
            text = message.get("text")
            if not text:
                continue
            if text.startswith("{"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    await asyncio.to_thread(
                        docker_ws.write_exec, session, text.encode("utf-8", errors="replace")
                    )
                    continue
                kind = payload.get("type")
                if kind == "resize":
                    await asyncio.to_thread(
                        docker_ws.resize_exec,
                        session.exec_id,
                        int(payload.get("cols") or 80),
                        int(payload.get("rows") or 24),
                    )
                elif kind == "input":
                    await asyncio.to_thread(
                        docker_ws.write_exec,
                        session,
                        str(payload.get("data") or "").encode("utf-8", errors="replace"),
                    )
            else:
                await asyncio.to_thread(
                    docker_ws.write_exec, session, text.encode("utf-8", errors="replace")
                )
    except WebSocketDisconnect:
        pass
    finally:
        session.close()
        pump.cancel()
        with suppress(asyncio.CancelledError):
            await pump
        with suppress(Exception):
            await websocket.close()
