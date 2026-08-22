from pathlib import Path
from socket import getaddrinfo
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _host_resolves(host: str, port: int | None) -> bool:
    try:
        getaddrinfo(host, port or 0)
        return True
    except OSError:
        return False


def _remote_docker_hostname() -> str | None:
    host = (settings.docker_host or "").strip()
    if not host:
        return None
    parsed = urlparse(host)
    if parsed.scheme in {"ssh", "tcp", "http", "https"}:
        return parsed.hostname
    return None


def resolve_database_url(raw: str) -> str:
    url = (raw or "").strip() or "sqlite:///./data/studio.db"

    # sqlite://user:pass@host:port/db 는 파일 SQLite가 아니라 MySQL 연결 형태
    if url.startswith("sqlite://") and "@" in url and not url.startswith("sqlite:///"):
        url = "mysql+pymysql://" + url[len("sqlite://") :]
    elif url.startswith("mysql://"):
        url = "mysql+pymysql://" + url[len("mysql://") :]

    parsed = make_url(url)
    backend = parsed.get_backend_name()

    if (
        parsed.host
        and not backend.startswith("sqlite")
        and not _host_resolves(parsed.host, parsed.port)
    ):
        remote = _remote_docker_hostname()
        if remote:
            parsed = parsed.set(host=remote)

    if backend.startswith("mysql") and "charset" not in parsed.query:
        parsed = parsed.update_query_dict({"charset": "utf8mb4"})

    return parsed.render_as_string(hide_password=False)


DATABASE_URL = resolve_database_url(settings.database_url)


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    backend = parsed.get_backend_name()

    if backend.startswith("sqlite"):
        database = parsed.database or ""
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        return

    if not parsed.database:
        return
    if not (backend.startswith("mysql") or backend.startswith("mariadb")):
        return

    admin_url = parsed.set(database="").render_as_string(hide_password=False)
    admin_engine = create_engine(admin_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{parsed.database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    except Exception:
        # 계정에 CREATE 권한이 없으면 이미 만들어진 DB에 바로 접속한다.
        pass
    finally:
        admin_engine.dispose()


def _engine_kwargs(url: str) -> dict:
    if make_url(url).get_backend_name().startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True, "pool_recycle": 3600}


engine = create_engine(DATABASE_URL, **_engine_kwargs(DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401

    _ensure_database(DATABASE_URL)
    Base.metadata.create_all(bind=engine)
    migrate()


def migrate() -> None:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    with engine.begin() as conn:
        if "workspaces" in tables:
            columns = {column["name"] for column in inspector.get_columns("workspaces")}
            extras = {
                "spec_id": "VARCHAR(16)",
                "memory_limit": "VARCHAR(16)",
                "pip_packages": "TEXT",
                "apt_packages": "TEXT",
                "docker_image": "VARCHAR(120)",
                "kind": "VARCHAR(16) DEFAULT 'vscode'",
                "env_json": "TEXT",
                "command_json": "TEXT",
            }
            for name, ddl in extras.items():
                if name not in columns:
                    conn.execute(text(f"ALTER TABLE workspaces ADD COLUMN {name} {ddl}"))
        if "specs" in tables:
            spec_cols = {column["name"] for column in inspector.get_columns("specs")}
            spec_extras = {
                "apt_packages": "TEXT",
                "kind": "VARCHAR(16) DEFAULT 'vscode'",
                "markdown": "TEXT",
                "env_json": "TEXT",
                "command_json": "TEXT",
            }
            for name, ddl in spec_extras.items():
                if name not in spec_cols:
                    conn.execute(text(f"ALTER TABLE specs ADD COLUMN {name} {ddl}"))
        if "conversations" in tables:
            convo_cols = {column["name"] for column in inspector.get_columns("conversations")}
            if "pending_spec_id" not in convo_cols:
                conn.execute(text("ALTER TABLE conversations ADD COLUMN pending_spec_id VARCHAR(16)"))
