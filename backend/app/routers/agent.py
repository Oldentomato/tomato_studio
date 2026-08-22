from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import ChatIn, ChatOut, SpecOut, WorkspaceOut
from ..services import agent, specs as spec_service

router = APIRouter(prefix="/api", tags=["agent"])


@router.get("/specs", response_model=list[SpecOut])
def list_specs(db: Session = Depends(get_db)) -> list[SpecOut]:
    return [agent.spec_out(item) for item in spec_service.list_specs(db)]


@router.get("/specs/{spec_id}", response_model=SpecOut)
def get_spec(spec_id: str, db: Session = Depends(get_db)) -> SpecOut:
    spec = spec_service.get_spec(db, spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="사양서를 찾을 수 없습니다.")
    return agent.spec_out(spec)


@router.post("/specs/{spec_id}/create", response_model=WorkspaceOut)
def create_from_spec(spec_id: str, db: Session = Depends(get_db)) -> WorkspaceOut:
    try:
        workspace, _, _ = agent.create_from_spec(db, spec_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return agent.workspace_out(workspace)


@router.post("/agent/chat", response_model=ChatOut)
def chat(payload: ChatIn, db: Session = Depends(get_db)) -> ChatOut:
    try:
        return agent.chat(db, payload.message, payload.conversation_id, payload.workspace_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/agent/chat/stream")
def chat_stream(payload: ChatIn, db: Session = Depends(get_db)):
    stream = agent.chat_stream(db, payload.message, payload.conversation_id, payload.workspace_id)
    return StreamingResponse(stream, media_type="text/event-stream")
