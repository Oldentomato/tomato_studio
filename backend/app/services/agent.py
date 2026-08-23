from __future__ import annotations

import json
import queue
import threading
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Conversation, Spec, Workspace, utcnow
from ..schemas import ChatOut, SpecOut, ToolTrace, WorkspaceOut
from . import docker_ws, specs as spec_service

SSE_PING_INTERVAL_SECONDS = 15.0

_WEB_PORT_HINT = ", ".join(
    f"{name}={port}" for name, port in sorted(spec_service.WEB_UI_PORTS.items())
)

SYSTEM_PROMPT = """당신은 Tomato Studio의 개발 환경 에이전트입니다.
사용자는 한국어로 개발 환경을 요청합니다. VS Code 워크스페이스, 일반 컨테이너, 웹 UI 컨테이너를 구분합니다.

도구:
1) write_spec: 요청을 사양서로 정리합니다. 구조화 필드와 마크다운 본문을 함께 채웁니다. 기존 사양서를 고칠 때는 spec_id를 넣습니다.
2) update_container: 이미 있는 워크스페이스에 사양서를 적용하고 다시 시작합니다. 새 컨테이너를 추가로 만들지 않습니다.
3) delete_container: 워크스페이스 id로 컨테이너와 볼륨을 삭제합니다.
4) lookup_containers: 이미 있는 다른 컨테이너의 접속 정보(id, 이름, hostname, 포트, env)를 조회합니다. query에 이름/id/slug/호스트명을 넣습니다. 비우면 목록입니다.

원칙 (절대):
- 컨테이너를 처음부터 만드는 도구는 없습니다. create_container를 호출하지 마세요.
- 새 환경을 만들어 달라는 요청이 오면 write_spec만 호출하고, 오른쪽 사양서를 확인하라고 안내합니다.
- "만들어줘"라고 해도 사양서만 작성합니다. 새 컨테이너는 사용자가 사양서의 **컨테이너 만들기** 버튼을 누를 때만 생깁니다.
- 새 환경을 만들 때는 update_container를 호출하지 마세요. write_spec만 하고 버튼을 안내하세요.
- update_container는 사용자가 기존 카드를 선택한 뒤, 그 카드의 workspace_id가 있을 때만 호출하세요. lookup으로 찾은 연동 대상 id를 update에 넣지 마세요.
- 예외: 사용자가 오류 난 카드를 고른 뒤 고쳐 달라고 하면, error/logs를 읽고 원인을 고친 다음 write_spec(spec_id=기존) 후 update_container(workspace_id=기존)를 호출하세요. 같은 값으로 재적용만 하지 마세요.
- 답변은 짧고 한국어로. 사양서 내용을 채팅에 반복하지 말고 오른쪽 패널을 보라고 안내합니다.
- kind: vscode, container, web.
  - vscode: code-server IDE. pip/apt는 여기에만 설치합니다.
  - container: 이미지를 그대로 실행하는 일반 컨테이너(DB, 캐시, 웹서버를 터미널로 쓰는 경우 등). 같은 tomato-studio 네트워크에서 tomato-ws-<id> 로 접근합니다. 열면 터미널입니다.
  - web: 브라우저로 들어가는 HTTP 화면. 터미널이 아니라 스튜디오 안에서 웹 UI로 엽니다. 대시보드, 관리 콘솔, 노트북 UI, 정적/관리 웹앱 등 HTTP를 듣는 이미지에 씁니다.
- docker_image는 Docker Hub 이미지. 예: python:3.12-slim, ubuntu:24.04, postgres:16, nginx:alpine, grafana/grafana:latest.
- 메모리: 512m, 1g, 2g, 4g, 8g.
- vscode일 때만 pip/apt를 채웁니다. container/web이면 pip_packages와 apt_packages는 빈 배열.
- kind=web 이면 http_port를 반드시 채웁니다. 이것은 컨테이너 **안에서** 웹 서버가 듣는 포트입니다. 호스트에 랜덤으로 열리는 공개 포트(host_port)가 아닙니다.
- 웹 이미지 기본 내부 포트: """ + _WEB_PORT_HINT + """
- 목록에 없는 이미지는 Docker Hub의 EXPOSE/문서 포트를 쓰세요. 추측으로 80을 넣지 마세요.
- 부가 요청(환경변수, 실행 인자, 비밀번호, 커스텀 프로세스, 베이스 패스 등)은 env와 command에 넣습니다. DB만이 아닙니다.
- markdown 실행 절에는 실제로 쓸 docker run 명령을 `실행 명령`으로 보여줍니다.
- 파일 올리기/받기는 도구가 없습니다. 사용자가 파일을 말하면 워크스페이스 카드의 **파일** 버튼을 안내하세요.
- 이미 만든 사양서를 고칠 때는 반드시 spec_id를 넣습니다.
- 사용자가 다른 컨테이너 이름/id를 말하거나, 기존 서비스에 붙이거나 연동하라고 하면 추측하지 말고 먼저 lookup_containers를 호출하세요.
- 다른 컨테이너에 접속할 때는 host_port가 아니라 hostname(`tomato-ws-<id>`)과 connect_port(컨테이너 내부 포트)를 쓰세요. 같은 tomato-studio 네트워크입니다.
- 조회한 env(계정, 비밀번호, DB 이름 등)와 포트를 사양서의 env/command/markdown에 그대로 반영하세요. 호스트명을 지어내지 마세요.

오류 자가진단:
- 선택한 카드의 error, logs, http_port, env, command, EXPOSE 힌트를 읽고 원인에 해당하는 필드만 바꿉니다.
- "웹 화면이 준비되지 않았습니다": 스튜디오가 호스트 공개 포트로 HTTP GET / 를 최대 90초 때렸는데 연결 실패이거나 5xx입니다. kind=web 컨테이너가 기동은 됐지만, 지정한 http_port에서 HTTP가 안 열린 상태입니다.
  1) 로그에 EXPOSE와 http_port가 다르면 http_port를 EXPOSE/공식 포트로 고칩니다. 로그의 host port N을 http_port로 복사하지 마세요.
  2) 컨테이너가 종료됐거나 로그에 missing password, cannot bind, permission, invalid config가 있으면 env 또는 command를 고칩니다.
  3) 앱이 127.0.0.1만 들으면 command에 0.0.0.0 bind를 넣습니다.
  4) HTTP UI가 아닌 이미지면 kind를 container로 바꾸거나 웹 UI 이미지로 바꿉니다.
  5) 원인을 못 찾으면 해당 이미지의 가장 흔한 포트로 http_port를 바꾸고, 그래도 같으면 env/command를 로그 기준으로 수정합니다. 동일한 spec으로 update만 반복하지 마세요.

""" + spec_service.SPEC_MARKDOWN_GUIDE + """
- 삭제 전에 현재 컨테이너 id를 확인합니다.
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
                        "enum": ["vscode", "container", "web"],
                        "description": "vscode=IDE, container=터미널로 여는 일반 컨테이너, web=브라우저 웹 UI",
                    },
                    "http_port": {
                        "type": "integer",
                        "description": "kind=web일 때 컨테이너 안에서 웹 서버가 듣는 포트. 호스트 공개 포트(host_port)가 아님. 이미지 EXPOSE/공식 포트에 맞출 것. grafana=3000, jupyter=8888, nginx=80, portainer=9000.",
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
                    "env": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "컨테이너 환경변수. 예: POSTGRES_DB=studio, REDIS_PASSWORD=secret",
                    },
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "이미지 기본 CMD 대신 쓸 실행 인자. 예: [\"redis-server\", \"--requirepass\", \"secret\"]",
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
            "description": "이미 있는 워크스페이스에 사양서를 적용하고 다시 시작합니다. 오류 수정/업데이트용입니다. workspace_id가 있는 기존 카드에만 쓰세요. 새 컨테이너를 만들 때는 호출하지 마세요.",
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
            "name": "lookup_containers",
            "description": "이미 있는 워크스페이스/컨테이너의 접속 정보를 조회합니다. 다른 서비스에 붙이거나 사양서를 맞출 때 먼저 호출하세요.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "이름, workspace id, slug, tomato-ws-<id>, 이미지 이름. 비우면 전체 목록.",
                    },
                },
            },
        },
    },
]

NEGATIVE_SPEC_HINTS = ("삭제", "지워", "중지", "다운로드", "파일", "업로드")
POSITIVE_SPEC_HINTS = (
    "사양",
    "환경",
    "python",
    "pandas",
    "fastapi",
    "ubuntu",
    "debian",
    "postgres",
    "redis",
    "세팅",
    "셋업",
    "웹",
    "대시보드",
    "grafana",
    "jupyter",
    "nginx",
)
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
        http_port=workspace.http_port,
        hostname=f"tomato-ws-{workspace.id}",
        access=spec_service.service_access(
            workspace.docker_image or "",
            workspace.id,
            workspace.slug,
            spec_service.parse_env(getattr(workspace, "env_json", None)),
            kind=workspace.kind or "vscode",
            http_port=workspace.http_port,
        )
        if (workspace.kind or "vscode") in {"container", "web"}
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
        http_port=data.get("http_port"),
        access=data.get("access"),
        notes=data["notes"],
        markdown=data.get("markdown") or "",
        workspace_id=data["workspace_id"],
        created_at=spec.created_at,
    )


def _listen_port(workspace: Workspace) -> int | None:
    kind = (workspace.kind or "vscode").strip().lower()
    if kind == "vscode":
        return 8080
    if kind == "web" and workspace.http_port:
        return int(workspace.http_port)
    access = spec_service.service_access(
        workspace.docker_image or "",
        workspace.id,
        workspace.slug,
        spec_service.parse_env(getattr(workspace, "env_json", None)),
        kind=kind,
        http_port=workspace.http_port,
    )
    port = (access or {}).get("port")
    if port:
        return int(port)
    if workspace.http_port:
        return int(workspace.http_port)
    return None


def _container_info(workspace: Workspace, *, detail: bool = False) -> dict[str, Any]:
    docker_ws.sync_from_docker(workspace)
    kind = (workspace.kind or "vscode").strip().lower() or "vscode"
    hostname = f"tomato-ws-{workspace.id}"
    aliases = [hostname]
    if workspace.slug and workspace.slug not in aliases:
        aliases.append(workspace.slug)
    connect_port = _listen_port(workspace)
    info: dict[str, Any] = {
        "workspace_id": workspace.id,
        "name": workspace.name,
        "slug": workspace.slug,
        "kind": kind,
        "status": workspace.status,
        "docker_image": workspace.docker_image,
        "hostname": hostname,
        "aliases": aliases,
        "network": "tomato-studio",
        "connect_port": connect_port,
        "http_port": workspace.http_port,
        "host_port": workspace.host_port,
        "spec_id": workspace.spec_id,
        "memory": workspace.memory_limit,
    }
    if detail:
        env = spec_service.parse_env(getattr(workspace, "env_json", None))
        command = spec_service.parse_command(getattr(workspace, "command_json", None))
        access = spec_service.service_access(
            workspace.docker_image or "",
            workspace.id,
            workspace.slug,
            env,
            kind=kind,
            http_port=workspace.http_port,
        )
        info["env"] = env
        info["command"] = command
        info["access"] = access
        info["error"] = workspace.error_message
        info["connect"] = f"{hostname}:{connect_port}" if connect_port else hostname
        info["note"] = (
            "같은 tomato-studio 네트워크의 다른 컨테이너는 hostname:connect_port 로 접속하세요. "
            "host_port는 브라우저/호스트용이며 컨테이너 간 통신에 쓰지 마세요."
        )
    return info


def _workspace_matches(workspace: Workspace, query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    hostname = f"tomato-ws-{workspace.id}"
    fields = [
        workspace.id,
        workspace.name or "",
        workspace.slug or "",
        hostname,
        workspace.docker_image or "",
        workspace.kind or "",
        workspace.spec_id or "",
    ]
    return any(q == item.lower() or q in item.lower() or item.lower() in q for item in fields if item)


def _lookup_containers(db: Session, query: str = "") -> list[dict[str, Any]]:
    rows = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
    matched = [item for item in rows if _workspace_matches(item, query)]
    if not query:
        matched = matched[:20]
    detail = bool(query) or len(matched) <= 5
    return [_container_info(item, detail=detail) for item in matched]


def _snapshot(db: Session) -> str:
    spec_rows = spec_service.list_specs(db)[:8]
    workspaces = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
    spec_lines = [
        f"- {item.id} | {item.name} | {item.kind or 'vscode'} | {item.docker_image} | {item.memory} | pip={spec_service.parse_packages(item.pip_packages)} | apt={spec_service.parse_packages(item.apt_packages)} | ws={item.workspace_id}"
        for item in spec_rows
    ] or ["- 없음"]
    ws_lines = []
    for item in workspaces[:16]:
        info = _container_info(item, detail=False)
        ws_lines.append(
            f"- {info['workspace_id']} | {info['name']} | {info['kind']} | {info['status']} | "
            f"host={info['hostname']} | port={info['connect_port'] or '-'} | image={info['docker_image'] or '-'} | "
            f"spec={info['spec_id'] or '-'}"
        )
    if not ws_lines:
        ws_lines = ["- 없음"]
    return "현재 사양서:\n" + "\n".join(spec_lines) + "\n현재 컨테이너:\n" + "\n".join(ws_lines)


def _mentioned_container_context(db: Session, message: str) -> str:
    text = (message or "").strip().lower()
    if not text:
        return ""
    found: list[Workspace] = []
    seen: set[str] = set()
    for item in db.query(Workspace).order_by(Workspace.created_at.desc()).all():
        needles = [item.id, f"tomato-ws-{item.id}", item.slug or "", item.name or ""]
        if any(needle and len(needle) >= 2 and needle.lower() in text for needle in needles):
            if item.id not in seen:
                seen.add(item.id)
                found.append(item)
    if not found:
        return ""
    records = [_container_info(item, detail=True) for item in found]
    return (
        "사용자가 메시지에서 언급한 컨테이너입니다. 사양서를 만들거나 고칠 때 이 접속 정보를 사용하세요. "
        "hostname:connect_port 로 붙이고 host_port는 쓰지 마세요.\n"
        + json.dumps(records, ensure_ascii=False, default=str)
    )


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
    logs = docker_ws.get_progress(workspace.id)[-40:]
    spec = spec_service.get_spec(db, workspace.spec_id) if workspace.spec_id else None
    env = spec_service.parse_env(getattr(workspace, "env_json", None))
    command = spec_service.parse_command(getattr(workspace, "command_json", None))
    lines = [
        "사용자가 오른쪽에서 선택한 워크스페이스입니다. 이 대상을 우선하세요.",
        f"- workspace_id: {workspace.id}",
        f"- name: {workspace.name}",
        f"- kind: {workspace.kind or 'vscode'}",
        f"- status: {workspace.status}",
        f"- docker_image: {workspace.docker_image or '-'}",
        f"- http_port: {workspace.http_port or '-'} (컨테이너 내부 리스닝 포트)",
        f"- host_port: {workspace.host_port or '-'} (호스트 공개 포트. http_port가 아님)",
        f"- env: {json.dumps(env, ensure_ascii=False) if env else '-'}",
        f"- command: {json.dumps(command, ensure_ascii=False) if command else '-'}",
        f"- spec_id: {workspace.spec_id or '없음'}",
        f"- error: {workspace.error_message or '없음'}",
    ]
    if logs:
        lines.append("- logs:")
        lines.extend(f"  {line}" for line in logs)
    if spec:
        spec_env = spec_service.parse_env(getattr(spec, "env_json", None))
        spec_cmd = spec_service.parse_command(getattr(spec, "command_json", None))
        lines.append(
            f"- 사양서: {spec.name} | {spec.kind} | {spec.docker_image} | {spec.memory} | "
            f"http_port={spec.http_port or '-'} | env={json.dumps(spec_env, ensure_ascii=False)} | "
            f"command={json.dumps(spec_cmd, ensure_ascii=False)}"
        )
    lines.append(
        "이 카드의 오류를 고쳐 업데이트하라면 error/logs를 보고 원인 필드를 바꾼 뒤 "
        "write_spec에 spec_id를 넣어 같은 사양서를 수정하고 update_container(workspace_id)를 호출하세요. "
        "같은 값으로 재적용하거나 새 컨테이너를 추가로 만들지 마세요."
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


HISTORY_LIMIT = 24


def _tool_call_ids(message: dict[str, Any]) -> set[str]:
    calls = message.get("tool_calls") or []
    return {str(call.get("id")) for call in calls if call.get("id")}


def _sanitize_history(messages: list[dict[str, Any]], *, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    """tool 메시지가 tool_calls 없는 assistant/system 뒤에 오지 않도록 맞춘다."""
    kept: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        if role == "tool":
            i += 1
            continue
        if role == "assistant" and msg.get("tool_calls"):
            ids = _tool_call_ids(msg)
            j = i + 1
            tools: list[dict[str, Any]] = []
            while j < n and messages[j].get("role") == "tool":
                tools.append(messages[j])
                j += 1
            matched = [item for item in tools if item.get("tool_call_id") in ids]
            if ids and len(matched) == len(ids):
                kept.append(msg)
                kept.extend(matched)
            i = j
            continue
        if role in {"user", "assistant", "system"}:
            kept.append(msg)
        i += 1

    if len(kept) <= limit:
        return kept

    start = len(kept) - limit
    while start < len(kept) and kept[start].get("role") == "tool":
        start += 1
    trimmed = kept[start:]
    if trimmed and trimmed[0].get("role") == "assistant" and trimmed[0].get("tool_calls"):
        ids = _tool_call_ids(trimmed[0])
        found: set[str] = set()
        k = 1
        while k < len(trimmed) and trimmed[k].get("role") == "tool":
            found.add(str(trimmed[k].get("tool_call_id")))
            k += 1
        if not ids or not ids <= found:
            trimmed = trimmed[k:]
            while trimmed and trimmed[0].get("role") == "tool":
                trimmed = trimmed[1:]
    return trimmed


def _messages(conversation: Conversation) -> list[dict[str, Any]]:
    try:
        data = json.loads(conversation.messages_json or "[]")
    except json.JSONDecodeError:
        data = []
    if not isinstance(data, list):
        return []
    return _sanitize_history([item for item in data if isinstance(item, dict)])


def _save_messages(db: Session, conversation: Conversation, messages: list[dict[str, Any]]) -> None:
    conversation.messages_json = json.dumps(_sanitize_history(messages), ensure_ascii=False)
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
                "markdown은 개요/실행/설치/접속/메모 템플릿. IDE면 kind=vscode, HTTP 웹 화면이면 kind=web+http_port, 그 외 이미지 그대로 실행이면 kind=container."
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
    env = spec_service.resolved_env(spec)
    command = spec_service.resolved_command(spec)
    if spec.workspace_id:
        existing = db.get(Workspace, spec.workspace_id)
        if existing is not None:
            image_changed = existing.docker_image and existing.docker_image != spec.docker_image
            kind_changed = (existing.kind or "vscode") != (spec.kind or "vscode")
            port_changed = (existing.http_port or None) != (spec.http_port or None)
            run_changed, env_changed = spec_service.run_config_changed(
                spec.docker_image,
                spec_service.parse_env(getattr(existing, "env_json", None)),
                env,
                spec_service.parse_command(getattr(existing, "command_json", None)),
                command,
            )
            if image_changed or kind_changed:
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
                existing.http_port = spec.http_port
                existing.env_json = json.dumps(env, ensure_ascii=False)
                existing.command_json = json.dumps(command, ensure_ascii=False)
                db.add(existing)
                db.commit()
                if (spec.kind or "vscode") in {"container", "web"} and (run_changed or port_changed):
                    workspace = docker_ws.recreate_workspace(db, existing, reset_volume=env_changed)
                else:
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
        env_json=json.dumps(env, ensure_ascii=False),
        command_json=json.dumps(command, ensure_ascii=False),
        http_port=spec.http_port,
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
            env=args.get("env") if isinstance(args.get("env"), dict) else None,
            command=args.get("command") if isinstance(args.get("command"), list) else None,
            http_port=args.get("http_port"),
        )
        payload = spec_service.spec_to_dict(spec)
        return payload, {"spec": spec}

    if name == "update_container":
        workspace_id = str(args.get("workspace_id") or "").strip() or (
            selected_workspace.id if selected_workspace else ""
        )
        workspace = db.get(Workspace, workspace_id) if workspace_id else None
        if workspace is None:
            raise ValueError(
                "새 컨테이너는 update_container로 만들 수 없습니다. "
                "write_spec만 호출하고 사용자에게 오른쪽 **컨테이너 만들기** 버튼을 안내하세요."
            )
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

    if name == "lookup_containers":
        query = str(args.get("query") or "").strip()
        records = _lookup_containers(db, query)
        if not records:
            return {
                "containers": [],
                "query": query,
                "message": "일치하는 컨테이너가 없습니다. query를 비우면 전체 목록을 볼 수 있습니다.",
            }, {}
        return {"containers": records, "query": query, "count": len(records)}, {}

    raise ValueError(f"알 수 없는 도구: {name}")


def _to_chat_out(conversation_id: str, reply: str, artifacts: dict[str, Any], traces: list[ToolTrace]) -> ChatOut:
    spec_obj = artifacts.get("spec")
    workspace_obj = artifacts.get("workspace")
    return ChatOut(
        conversation_id=conversation_id,
        reply=reply,
        spec=spec_out(spec_obj) if spec_obj is not None else None,
        workspace=workspace_out(workspace_obj) if workspace_obj is not None else None,
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
    mentioned_text = _mentioned_container_context(db, message)
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
    if mentioned_text:
        openai_messages.append({"role": "system", "content": mentioned_text})
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
                    ok=True,
                    summary="새 컨테이너는 사양서의 '컨테이너 만들기' 버튼으로 생성합니다.",
                )
                traces.append(trace)
                content = json.dumps(
                    {
                        "skipped": True,
                        "message": "컨테이너 생성은 오른쪽 사양서 확인 후 버튼으로만 가능합니다. write_spec만 하세요.",
                    },
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

            if call.function.name == "update_container":
                workspace_id = str(args.get("workspace_id") or "").strip() or (
                    selected_workspace.id if selected_workspace else ""
                )
                target = db.get(Workspace, workspace_id) if workspace_id else None
                if target is None:
                    trace = ToolTrace(
                        name="update_container",
                        ok=True,
                        summary="새 컨테이너는 사양서의 '컨테이너 만들기' 버튼으로 생성합니다.",
                    )
                    traces.append(trace)
                    content = json.dumps(
                        {
                            "skipped": True,
                            "message": "대상 워크스페이스가 없습니다. 새 환경은 write_spec만 하고 버튼을 안내하세요.",
                        },
                        ensure_ascii=False,
                    )
                    if emit:
                        emit("tool_result", trace.model_dump(mode="json"))
                    tool_msg = {"role": "tool", "tool_call_id": call.id, "content": content}
                    openai_messages.append(tool_msg)
                    history.append(tool_msg)
                    continue

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
                history.append({"role": "assistant", "content": reply})
                _save_messages(db, conversation, history)
                return _to_chat_out(conversation.id, reply, artifacts, traces)
            if not repair:
                reply = (
                    "사양서를 작성했습니다. 오른쪽에서 Docker 이미지, 메모리, pip/apt 목록을 확인한 뒤 "
                    "**컨테이너 만들기** 버튼을 눌러 주세요."
                )
                history.append({"role": "assistant", "content": reply})
                _save_messages(db, conversation, history)
                return _to_chat_out(conversation.id, reply, artifacts, traces)

    reply = "도구 호출이 너무 길어져 여기서 멈췄습니다. 이어서 말씀해 주세요."
    history.append({"role": "assistant", "content": reply})
    _save_messages(db, conversation, history)
    return _to_chat_out(
        conversation.id,
        reply,
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
    message: str,
    conversation_id: str | None,
    workspace_id: str | None = None,
):
    def _encode(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    def _generator():
        events: queue.Queue[str | None] = queue.Queue()

        def emit(event: str, data: dict[str, Any]) -> None:
            events.put(_encode(event, data))

        def run() -> None:
            db = SessionLocal()
            try:
                result = _chat_core(
                    db,
                    message,
                    conversation_id,
                    emit=emit,
                    workspace_id=workspace_id,
                )
                events.put(_encode("done", result.model_dump(mode="json")))
            except Exception as exc:
                events.put(_encode("error", {"message": str(exc)}))
            finally:
                db.close()
                events.put(None)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        yield ": ping\n\n"
        while True:
            try:
                item = events.get(timeout=SSE_PING_INTERVAL_SECONDS)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield item

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
    if name == "lookup_containers":
        count = result.get("count") or len(result.get("containers") or [])
        query = result.get("query") or "전체"
        return f"조회 {count}개 · {query}"
    return name
