from datetime import datetime

from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    url: str | None
    host_port: int | None
    error_message: str | None
    spec_id: str | None = None
    memory_limit: str | None = None
    pip_packages: list[str] = []
    apt_packages: list[str] = []
    docker_image: str | None = None
    kind: str = "vscode"
    hostname: str | None = None
    access: dict | None = None
    logs: list[str] = []
    created_at: datetime
    last_accessed_at: datetime

    model_config = {"from_attributes": True}


class SpecOut(BaseModel):
    id: str
    name: str
    summary: str
    docker_image: str
    memory: str
    python_version: str
    pip_packages: list[str]
    apt_packages: list[str] = []
    kind: str = "vscode"
    access: dict | None = None
    notes: str
    markdown: str = ""
    workspace_id: str | None
    created_at: datetime


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None
    workspace_id: str | None = None


class ToolTrace(BaseModel):
    name: str
    ok: bool
    summary: str


class DownloadOut(BaseModel):
    filename: str
    url: str
    path: str


class ChatOut(BaseModel):
    conversation_id: str
    reply: str
    spec: SpecOut | None = None
    workspace: WorkspaceOut | None = None
    download: DownloadOut | None = None
    tools: list[ToolTrace] = []
