from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    volume_name: Mapped[str] = mapped_column(String(160))
    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stopped")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    memory_limit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pip_packages: Mapped[str | None] = mapped_column(Text, nullable=True)
    apt_packages: Mapped[str | None] = mapped_column(Text, nullable=True)
    docker_image: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="vscode")
    env_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Spec(Base):
    __tablename__ = "specs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, default="")
    docker_image: Mapped[str] = mapped_column(String(120))
    memory: Mapped[str] = mapped_column(String(16))
    python_version: Mapped[str] = mapped_column(String(16), default="3.12")
    pip_packages: Mapped[str] = mapped_column(Text, default="[]")
    apt_packages: Mapped[str] = mapped_column(Text, default="[]")
    kind: Mapped[str] = mapped_column(String(16), default="vscode")
    notes: Mapped[str] = mapped_column(Text, default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    env_json: Mapped[str] = mapped_column(Text, default="{}")
    command_json: Mapped[str] = mapped_column(Text, default="[]")
    workspace_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    messages_json: Mapped[str] = mapped_column(Text, default="[]")
    pending_spec_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
