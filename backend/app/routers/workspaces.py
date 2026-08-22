import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import Workspace
from ..schemas import WorkspaceCreate, WorkspaceOut
from ..services import docker_ws
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
