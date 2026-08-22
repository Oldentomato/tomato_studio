from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Conversation, Spec, Workspace, utcnow
from ..schemas import ChatOut, DownloadOut, SpecOut, ToolTrace, WorkspaceOut
from . import docker_ws, specs as spec_service, volume_files

SYSTEM_PROMPT = """당신은 Tomato Studio의 개발 환경 에이전트입니다.
사용자는 한국어로 개발 환경을 요청합니다. VS Code 워크스페이스와 일반 컨테이너를 구분합니다.

도구:
1) write_spec: 요청을 사양서로 정리합니다. 구조화 필드와 마크다운 본문을 함께 채웁니다. 기존 사양서를 고칠 때는 spec_id를 넣습니다.
2) update_container: 이미 있는 워크스페이스에 사양서를 적용하고 다시 시작합니다. 새 컨테이너를 추가로 만들지 않습니다.
3) delete_container: 워크스페이스 id로 컨테이너와 볼륨을 삭제합니다.
4) download_file: 워크스페이스 볼륨에서 파일을 꺼냅니다. 디렉터리 경로를 주면 목록을 돌려줍니다.

원칙 (절대):
- 컨테이너를 처음부터 만드는 도구는 없습니다. create_container를 호출하지 마세요.
- 새 환경을 만들어 달라는 요청이 오면 write_spec만 호출하고, 오른쪽 사양서를 확인하라고 안내합니다.
- "만들어줘"라고 해도 사양서만 작성합니다. 새 컨테이너는 사용자가 사양서의 **컨테이너 만들기** 버튼을 누를 때만 생깁니다.
- 예외: 사용자가 오류 난 카드를 고른 뒤 고쳐 달라고 하면 write_spec(spec_id=기존) 후 update_container(workspace_id=기존)를 호출하세요.
- 답변은 짧고 한국어로. 사양서 내용을 채팅에 반복하지 말고 오른쪽 패널을 보라고 안내합니다.
- kind: vscode 또는 container.
  - vscode: code-server IDE. pip/apt는 여기에만 설치합니다.
  - container: 이미지를 그대로 실행하는 일반 컨테이너(DB, 캐시, 웹서버, 직접 만든 서비스 등). 같은 tomato-studio 네트워크에서 tomato-ws-<id> 로 접근합니다.
- docker_image는 Docker Hub 이미지. 예: python:3.12-slim, ubuntu:24.04, postgres:16, nginx:alpine.
- 메모리: 512m, 1g, 2g, 4g, 8g.
- vscode일 때만 pip/apt를 채웁니다. container면 pip_packages와 apt_packages는 빈 배열.
- markdown은 아래 템플릿을 따르세요. 절을 마음대로 추가하지 마세요. 필요 없는 절은 생략하세요.

""" + spec_service.SPEC_MARKDOWN_GUIDE + """
- 삭제/다운로드 전에 현재 컨테이너 id를 확인합니다.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_spec",
            "description": "사용자 요청에 맞는 개발 환경 사양서를 작성하고 화면에 보여 줍니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "짧은 환경 이름"},
                    "summary": {"type": "string", "description": "무엇을 위한 환경인지"},
                    "docker_image": {
                        "type": "string",
                        "description": "Docker 이미지. 예: python:3.12-slim, ubuntu:24.04, postgres:16, redis:7-alpine, mysql:8.0",
                    },
                    "memory": {"type": "string", "enum": sorted(spec_service.ALLOWED_MEMORY)},
                    "pip_packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "pip install 할 패키지 목록. 예: pandas, fastapi, jupyter, psycopg2",
                    },
                    "apt_packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "사용자가 요청한 시스템 패키지. 예: postgresql, redis-server. pip용 libpq-dev 등은 비워도 시스템이 자동 추가합니다.",
                    },
                    "python_version": {"type": "string", "description": "예: 3.12. vscode일 때만 의미 있음"},
                    "kind": {
                        "type": "string",
                        "enum": ["vscode", "container"],
                        "description": "vscode=IDE, container=이미지를 그대로 실행하는 일반 컨테이너",
                    },
                    "notes": {"type": "string", "description": "짧게 남을 주의점. markdown 메모 절과 맞춰도 됨"},
                    "markdown": {
                        "type": "string",
                        "description": "템플릿을 따른 마크다운 사양서. 절: 개요, 실행, 설치, 접속, 메모",
                    },
                    "spec_id": {
                        "type": "string",
                        "description": "기존 사양서를 수정할 때 그 id. 새 사양서를 만들 때는 비웁니다.",
                    },
                },
                "required": ["name", "summary", "docker_image", "memory", "kind", "pip_packages", "apt_packages", "markdown"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_container",
            "description": "이미 있는 워크스페이스에 사양서를 적용하고 다시 시작합니다. 오류 수정/업데이트용입니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string", "description": "업데이트할 워크스페이스 id"},
                    "spec_id": {"type": "string", "description": "적용할 사양서 id. 비우면 워크스페이스에 연결된 사양서"},
                },
                "required": ["workspace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_container",
            "description": "워크스페이스 컨테이너와 볼륨을 삭제합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                },
                "required": ["workspace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "컨테이너 볼륨에서 파일을 다운로드하거나, 폴더면 목록을 보여 줍니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_id": {"type": "string"},
                    "path": {
                        "type": "string",
                        "description": "프로젝트 루트 기준 상대 경로. 비우거나 . 이면 목록.",
                    },
                },
                "required": ["workspace_id", "path"],
            },
        },
    },
]

NEGATIVE_SPEC_HINTS = ("삭제", "지워", "중지", "다운로드", "파일", "목록")
POSITIVE_SPEC_HINTS = ("사양", "환경", "python", "pandas", "fastapi", "ubuntu", "debian", "postgres", "redis", "세팅", "셋업")
REPAIR_HINTS = ("고쳐", "수정", "업데이트", "다시 만", "재시작", "반영해")


def workspace_out(workspace: Workspace, *, sync: bool = True) -> WorkspaceOut:
    if sync:
        docker_ws.sync_from_docker(workspace)
    try:
        packages = json.loads(workspace.pip_packages or "[]")
    except json.JSONDecodeError:
        packages = []
    try:
        apt_packages = json.loads(workspace.apt_packages or "[]")
    except json.JSONDecodeError:
        apt_packages = []
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        status=workspace.status,
        url=docker_ws.public_url(workspace),
        host_port=workspace.host_port,
        error_message=workspace.error_message,
        spec_id=workspace.spec_id,
        memory_limit=workspace.memory_limit,
        pip_packages=packages if isinstance(packages, list) else [],
        apt_packages=apt_packages if isinstance(apt_packages, list) else [],
        docker_image=workspace.docker_image,
        kind=workspace.kind or "vscode",
        hostname=f"tomato-ws-{workspace.id}",
        access=spec_service.service_access(workspace.docker_image or "", workspace.id, workspace.slug)
        if (workspace.kind or "vscode") == "container"
        else None,
        logs=docker_ws.get_progress(workspace.id),
        created_at=workspace.created_at,
        last_accessed_at=workspace.last_accessed_at,
    )


def spec_out(spec) -> SpecOut:
    data = spec_service.spec_to_dict(spec)
    return SpecOut(
        id=data["id"],
        name=data["name"],
        summary=data["summary"],
        docker_image=data["docker_image"],
        memory=data["memory"],
        python_version=data["python_version"],
        pip_packages=data["pip_packages"],
        apt_packages=data["apt_packages"],
        kind=data.get("kind") or "vscode",
        access=data.get("access"),
        notes=data["notes"],
        markdown=data.get("markdown") or "",
        workspace_id=data["workspace_id"],
        created_at=spec.created_at,
    )


def _snapshot(db: Session) -> str:
    spec_rows = spec_service.list_specs(db)[:8]
    workspaces = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
    spec_lines = [
        f"- {item.id} | {item.name} | {item.kind or 'vscode'} | {item.docker_image} | {item.memory} | pip={spec_service.parse_packages(item.pip_packages)} | apt={spec_service.parse_packages(item.apt_packages)} | ws={item.workspace_id}"
        for item in spec_rows
    ] or ["- 없음"]
    ws_lines = []
    for item in workspaces[:8]:
        docker_ws.sync_from_docker(item)
        ws_lines.append(
            f"- {item.id} | {item.name} | {item.status} | spec={item.spec_id} | err={item.error_message or '-'}"
        )
    if not ws_lines:
        ws_lines = ["- 없음"]
    return "현재 사양서:\n" + "\n".join(spec_lines) + "\n현재 컨테이너:\n" + "\n".join(ws_lines)


def _is_repair_request(message: str) -> bool:
    text = message or ""
    return any(token in text for token in REPAIR_HINTS)


def _selected_context(db: Session, workspace_id: str | None) -> tuple[str, Workspace | None]:
    if not workspace_id:
        return "", None
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        return "선택한 워크스페이스를 찾을 수 없습니다.", None
    docker_ws.sync_from_docker(workspace)
    logs = docker_ws.get_progress(workspace.id)[-15:]
    spec = spec_service.get_spec(db, workspace.spec_id) if workspace.spec_id else None
    lines = [
        "사용자가 오른쪽에서 선택한 워크스페이스입니다. 이 대상을 우선하세요.",
        f"- workspace_id: {workspace.id}",
        f"- name: {workspace.name}",
        f"- kind: {workspace.kind or 'vscode'}",
        f"- status: {workspace.status}",
        f"- docker_image: {workspace.docker_image or '-'}",
        f"- spec_id: {workspace.spec_id or '없음'}",
        f"- error: {workspace.error_message or '없음'}",
    ]
    if logs:
        lines.append("- logs:")
        lines.extend(f"  {line}" for line in logs)
    if spec:
        lines.append(
            f"- 사양서: {spec.name} | {spec.kind} | {spec.docker_image} | {spec.memory}"
        )
    lines.append(
        "이 카드의 오류를 고쳐 업데이트하라면 write_spec에 spec_id를 넣어 같은 사양서를 수정한 뒤 "
        "update_container(workspace_id)를 호출하세요. 새 컨테이너를 추가로 만들지 마세요."
    )
    return "\n".join(lines), workspace


def _load_conversation(db: Session, conversation_id: str | None) -> Conversation:
    if conversation_id:
        found = db.get(Conversation, conversation_id)
        if found:
            return found
    item = Conversation(id=docker_ws.new_id(), messages_json="[]")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _messages(conversation: Conversation) -> list[dict[str, Any]]:
    try:
        data = json.loads(conversation.messages_json or "[]")
    except json.JSONDecodeError:
        data = []
    return data[-24:]


def _save_messages(db: Session, conversation: Conversation, messages: list[dict[str, Any]]) -> None:
    conversation.messages_json = json.dumps(messages[-24:], ensure_ascii=False)
    conversation.updated_at = utcnow()
    db.add(conversation)
    db.commit()


def _should_force_spec(message: str, artifacts: dict[str, Any], *, repair: bool = False) -> bool:
    if artifacts.get("spec") is not None:
        return False
    text = (message or "").lower()
    if any(token in text for token in NEGATIVE_SPEC_HINTS):
        return False
    if repair:
        return True
    return any(token in text for token in POSITIVE_SPEC_HINTS)


def _force_write_spec_with_llm(
    db: Session,
    client: OpenAI,
    openai_messages: list[dict[str, Any]],
    *,
    spec_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    extra_hint = ""
    if spec_id:
        extra_hint = f" 반드시 spec_id={spec_id} 를 넣어 기존 사양서를 수정하세요."
    forced_messages = [
        *openai_messages,
        {
            "role": "system",
            "content": (
                "방금 요청은 사양서 작성 의도입니다. 답변 텍스트 대신 반드시 write_spec 도구를 호출하세요. "
                "name, summary, docker_image, memory, kind, pip_packages, apt_packages, markdown을 모두 채우세요. "
                "markdown은 개요/실행/설치/접속/메모 템플릿. IDE면 kind=vscode, 그 외 이미지 그대로 실행이면 kind=container."
                f"{extra_hint}"
            ),
        },
    ]
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=forced_messages,
        tools=TOOLS,
        tool_choice={"type": "function", "function": {"name": "write_spec"}},
    )
    msg = response.choices[0].message
    if not msg.tool_calls:
        return None
    call = msg.tool_calls[0]
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    if spec_id and not args.get("spec_id"):
        args["spec_id"] = spec_id
    return _run_tool(db, "write_spec", args, repair=bool(spec_id))


def create_from_spec(db: Session, spec_id: str) -> tuple[Workspace, Spec, bool]:
    spec = spec_service.get_spec(db, spec_id)
    if spec is None:
        raise ValueError("사양서를 찾을 수 없습니다.")
    if spec.workspace_id:
        existing = db.get(Workspace, spec.workspace_id)
        if existing is not None:
            image_changed = existing.docker_image and existing.docker_image != spec.docker_image
            kind_changed = (existing.kind or "vscode") != (spec.kind or "vscode")
            if image_changed or kind_changed:
                # 이미지가 다르면 기존 컨테이너를 삭제하고 새로 만든다
                docker_ws.delete_workspace(db, existing)
                spec.workspace_id = None
                db.add(spec)
                db.commit()
            else:
                existing.pip_packages = spec.pip_packages
                existing.apt_packages = spec.apt_packages
                existing.memory_limit = spec.memory
                existing.docker_image = spec.docker_image
                existing.kind = spec.kind or "vscode"
                db.add(existing)
                db.commit()
                workspace = docker_ws.start_workspace(
                    db, existing, prepare_python=(spec.kind or "vscode") == "vscode"
                )
                return workspace, spec, True

    packages = spec_service.parse_packages(spec.pip_packages)
    apt_packages = spec_service.parse_packages(spec.apt_packages)
    workspace = docker_ws.create_workspace(
        db,
        spec.name,
        spec_id=spec.id,
        memory_limit=spec.memory,
        pip_packages=packages,
        apt_packages=apt_packages,
        docker_image=spec.docker_image,
        kind=spec.kind or "vscode",
    )
    spec_service.touch_spec_workspace(db, spec, workspace.id)
    workspace = docker_ws.start_workspace(
        db, workspace, prepare_python=(spec.kind or "vscode") == "vscode"
    )
    return workspace, spec, False


def _remember_pending_spec(db: Session, conversation: Conversation, extra: dict[str, Any]) -> None:
    spec = extra.get("spec")
    if spec is None:
        return
    conversation.pending_spec_id = spec.id
    db.add(conversation)
    db.commit()


def _run_tool(
    db: Session,
    name: str,
    args: dict[str, Any],
    *,
    selected_workspace: Workspace | None = None,
    repair: bool = False,
    latest_spec: Spec | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if name == "write_spec":
        spec_id = args.get("spec_id") or ""
        if not spec_id and repair and selected_workspace and selected_workspace.spec_id:
            spec_id = selected_workspace.spec_id
        spec = spec_service.write_spec(
            db,
            name=args.get("name") or "python-env",
            summary=args.get("summary") or "",
            docker_image=args.get("docker_image") or "python:3.12-slim",
            memory=args.get("memory") or "1g",
            pip_packages=args.get("pip_packages") or [],
            apt_packages=args.get("apt_packages") or [],
            python_version=args.get("python_version"),
            kind=args.get("kind"),
            notes=args.get("notes") or "",
            markdown=args.get("markdown") or "",
            spec_id=spec_id or None,
        )
        payload = spec_service.spec_to_dict(spec)
        return payload, {"spec": spec}

    if name == "update_container":
        workspace_id = args.get("workspace_id") or (selected_workspace.id if selected_workspace else "")
        workspace = db.get(Workspace, workspace_id) if workspace_id else None
        if workspace is None:
            raise ValueError("업데이트할 워크스페이스가 없습니다. 오른쪽 카드를 선택한 뒤 요청하세요.")
        spec_id = args.get("spec_id") or (latest_spec.id if latest_spec is not None else "") or workspace.spec_id
        spec = spec_service.get_spec(db, spec_id) if spec_id else None
        if spec is None:
            raise ValueError("적용할 사양서가 없습니다. 먼저 사양서를 수정하세요.")
        spec.workspace_id = workspace.id
        workspace.spec_id = spec.id
        db.add(spec)
        db.add(workspace)
        db.commit()
        workspace, spec, updated = create_from_spec(db, spec.id)
        payload = {
            "workspace": {"id": workspace.id, "status": workspace.status, "name": workspace.name},
            "spec_id": spec.id,
            "updated": updated,
        }
        return payload, {"spec": spec, "workspace": workspace}

    if name == "create_container":
        raise ValueError("컨테이너는 오른쪽 사양서의 '컨테이너 만들기' 버튼으로만 생성할 수 있습니다.")

    if name == "delete_container":
        workspace_id = args.get("workspace_id") or ""
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError("컨테이너를 찾을 수 없습니다.")
        name_label = workspace.name
        for spec in db.query(Spec).filter(Spec.workspace_id == workspace_id).all():
            spec.workspace_id = None
        docker_ws.delete_workspace(db, workspace)
        return {"deleted": workspace_id, "name": name_label}, {"deleted_id": workspace_id}

    if name == "download_file":
        workspace_id = args.get("workspace_id") or ""
        path = args.get("path") or ""
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError("컨테이너를 찾을 수 없습니다.")
        rel = volume_files.safe_relpath(path)
        if not rel or path.strip() in {".", "/", ""}:
            listing = volume_files.list_paths(workspace.volume_name, rel)
            return {"type": "listing", "path": rel or ".", "files": listing}, {}
        try:
            filename, payload = volume_files.get_bytes(workspace.volume_name, rel)
        except FileNotFoundError:
            listing = volume_files.list_paths(workspace.volume_name, rel)
            return {
                "type": "listing",
                "path": rel,
                "files": listing,
                "error": "파일이 아니라 폴더이거나 경로를 찾지 못했습니다.",
            }, {}
        token = volume_files.save_download(filename, payload)
        download = {
            "filename": filename,
            "url": f"/api/downloads/{token}",
            "path": rel,
        }
        return {"type": "file", **download}, {"download": download}

    raise ValueError(f"알 수 없는 도구: {name}")


def _to_chat_out(conversation_id: str, reply: str, artifacts: dict[str, Any], traces: list[ToolTrace]) -> ChatOut:
    spec_obj = artifacts.get("spec")
    workspace_obj = artifacts.get("workspace")
    download_obj = artifacts.get("download")
    return ChatOut(
        conversation_id=conversation_id,
        reply=reply,
        spec=spec_out(spec_obj) if spec_obj is not None else None,
        workspace=workspace_out(workspace_obj) if workspace_obj is not None else None,
        download=DownloadOut(**download_obj) if download_obj else None,
        tools=traces,
    )


def _chat_core(
    db: Session,
    message: str,
    conversation_id: str | None,
    emit: Any | None = None,
    workspace_id: str | None = None,
) -> ChatOut:
    api_key = settings.resolved_openai_key
    if not api_key:
        raise RuntimeError("OpenAI API 키가 없습니다. backend/.env에 TOMATO_OPENAI_API_KEY를 넣으세요.")

    conversation = _load_conversation(db, conversation_id)
    history = _messages(conversation)
    history.append({"role": "user", "content": message})
    focus_text, selected_workspace = _selected_context(db, workspace_id)
    repair = bool(selected_workspace) and _is_repair_request(message)

    if emit:
        emit("conversation", {"conversation_id": conversation.id})

    client = OpenAI(api_key=api_key)
    openai_messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": _snapshot(db)},
    ]
    if focus_text:
        openai_messages.append({"role": "system", "content": focus_text})
    openai_messages.extend(history)

    artifacts: dict[str, Any] = {}
    traces: list[ToolTrace] = []
    spec_written_this_request = False

    def run_named(tool_name: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return _run_tool(
            db,
            tool_name,
            args,
            selected_workspace=selected_workspace,
            repair=repair,
            latest_spec=artifacts.get("spec"),
        )

    def apply_tool_result(tool_name: str, result: dict[str, Any], extra: dict[str, Any]) -> ToolTrace:
        nonlocal spec_written_this_request
        if tool_name == "write_spec":
            spec_written_this_request = True
        trace = ToolTrace(name=tool_name, ok=True, summary=_trace_summary(tool_name, result))
        traces.append(trace)
        artifacts.update(extra)
        _remember_pending_spec(db, conversation, extra)
        if emit:
            emit("tool_result", trace.model_dump(mode="json"))
            if extra.get("spec") is not None:
                emit("spec", spec_out(extra["spec"]).model_dump(mode="json"))
            if extra.get("workspace") is not None:
                emit("workspace", workspace_out(extra["workspace"]).model_dump(mode="json"))
            if extra.get("download") is not None:
                emit("download", extra["download"])
        return trace

    def auto_update_if_needed() -> None:
        if not repair or not selected_workspace:
            return
        if artifacts.get("workspace") is not None:
            return
        spec = artifacts.get("spec")
        spec_id = spec.id if spec is not None else selected_workspace.spec_id
        if not spec_id:
            return
        if emit:
            emit("tool_start", {"name": "update_container"})
        try:
            result, extra = run_named(
                "update_container",
                {"workspace_id": selected_workspace.id, "spec_id": spec_id},
            )
            apply_tool_result("update_container", result, extra)
        except Exception as exc:
            trace = ToolTrace(name="update_container", ok=False, summary=str(exc))
            traces.append(trace)
            if emit:
                emit("tool_result", trace.model_dump(mode="json"))

    for _ in range(6):
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=openai_messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        choice = response.choices[0]
        assistant = choice.message
        payload: dict[str, Any] = {"role": "assistant", "content": assistant.content or ""}
        if assistant.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                for call in assistant.tool_calls
            ]
        openai_messages.append(payload)
        history.append(payload)

        if choice.finish_reason != "tool_calls" or not assistant.tool_calls:
            if _should_force_spec(message, artifacts, repair=repair):
                forced = _force_write_spec_with_llm(
                    db,
                    client,
                    openai_messages,
                    spec_id=selected_workspace.spec_id if repair and selected_workspace else None,
                )
                if forced is not None:
                    result, extra = forced
                    apply_tool_result("write_spec", result, extra)
            auto_update_if_needed()
            reply = assistant.content or "작업했습니다."
            if artifacts.get("workspace") is not None and repair:
                reply = "사양서를 반영해 컨테이너를 다시 적용했습니다. 오른쪽 카드 상태를 확인해 주세요."
            elif (spec_written_this_request or artifacts.get("spec") is not None) and not artifacts.get("workspace"):
                reply = (
                    "사양서를 작성했습니다. 오른쪽에서 Docker 이미지, 메모리, pip/apt 목록을 확인한 뒤 "
                    "**컨테이너 만들기** 버튼을 눌러 주세요."
                )
            _save_messages(db, conversation, history)
            return _to_chat_out(conversation.id, reply, artifacts, traces)

        for call in assistant.tool_calls:
            if emit:
                emit("tool_start", {"name": call.function.name})

            if call.function.name == "create_container":
                trace = ToolTrace(
                    name="create_container",
                    ok=False,
                    summary="컨테이너는 사양서의 '컨테이너 만들기' 버튼으로만 생성합니다.",
                )
                traces.append(trace)
                content = json.dumps(
                    {"error": "컨테이너 생성은 오른쪽 사양서 확인 후 버튼으로만 가능합니다."},
                    ensure_ascii=False,
                )
                if emit:
                    emit("tool_result", trace.model_dump(mode="json"))
                tool_msg = {"role": "tool", "tool_call_id": call.id, "content": content}
                openai_messages.append(tool_msg)
                history.append(tool_msg)
                continue

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            try:
                result, extra = run_named(call.function.name, args)
                apply_tool_result(call.function.name, result, extra)
                content = json.dumps(result, ensure_ascii=False, default=str)
            except Exception as exc:
                trace = ToolTrace(name=call.function.name, ok=False, summary=str(exc))
                traces.append(trace)
                content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                if emit:
                    emit("tool_result", trace.model_dump(mode="json"))

            tool_msg = {"role": "tool", "tool_call_id": call.id, "content": content[:8000]}
            openai_messages.append(tool_msg)
            history.append(tool_msg)

        if spec_written_this_request and not artifacts.get("workspace"):
            auto_update_if_needed()
            if artifacts.get("workspace") is not None:
                reply = "사양서를 반영해 컨테이너를 다시 적용했습니다. 오른쪽 카드 상태를 확인해 주세요."
                _save_messages(db, conversation, history)
                return _to_chat_out(conversation.id, reply, artifacts, traces)
            if not repair:
                reply = (
                    "사양서를 작성했습니다. 오른쪽에서 Docker 이미지, 메모리, pip/apt 목록을 확인한 뒤 "
                    "**컨테이너 만들기** 버튼을 눌러 주세요."
                )
                _save_messages(db, conversation, history)
                return _to_chat_out(conversation.id, reply, artifacts, traces)

    _save_messages(db, conversation, history)
    return _to_chat_out(
        conversation.id,
        "도구 호출이 너무 길어져 여기서 멈췄습니다. 이어서 말씀해 주세요.",
        artifacts,
        traces,
    )


def chat(
    db: Session,
    message: str,
    conversation_id: str | None,
    workspace_id: str | None = None,
) -> ChatOut:
    return _chat_core(db, message, conversation_id, emit=None, workspace_id=workspace_id)


def chat_stream(
    db: Session,
    message: str,
    conversation_id: str | None,
    workspace_id: str | None = None,
):
    def _encode(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    def _generator():
        emitted: list[str] = []

        def emit(event: str, data: dict[str, Any]) -> None:
            emitted.append(_encode(event, data))

        try:
            result = _chat_core(
                db,
                message,
                conversation_id,
                emit=emit,
                workspace_id=workspace_id,
            )
            yield from emitted
            yield _encode("done", result.model_dump(mode="json"))
        except Exception as exc:
            yield from emitted
            yield _encode("error", {"message": str(exc)})

    return _generator()


def _trace_summary(name: str, result: dict[str, Any]) -> str:
    if name == "write_spec":
        return f"사양서 {result.get('id')} · {result.get('docker_image')} · {result.get('memory')}"
    if name == "update_container":
        ws = result.get("workspace") or {}
        return f"업데이트 {ws.get('id')} · {ws.get('status')}"
    if name == "create_container":
        ws = result.get("workspace") or {}
        return f"컨테이너 {ws.get('id')} · {ws.get('status')}"
    if name == "delete_container":
        return f"삭제 {result.get('deleted')}"
    if name == "download_file":
        if result.get("type") == "file":
            return f"다운로드 {result.get('filename')}"
        return f"목록 {len(result.get('files') or [])}개"
    return name
