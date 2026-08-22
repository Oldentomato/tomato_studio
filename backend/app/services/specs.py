from __future__ import annotations

import json
import re
import shlex

from sqlalchemy.orm import Session

from ..models import Spec, utcnow
from .docker_ws import new_id

ALLOWED_MEMORY = {"512m", "1g", "2g", "4g", "8g"}
ALLOWED_KINDS = {"vscode", "container"}
IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)*:[a-z0-9._-]+$")
PIP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,_-]+\])?(?:\s*(?:[=<>!~]=?|===)\s*[A-Za-z0-9._*+-]+)?$")
APT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]*(?:=[A-Za-z0-9.+~:-]+)?$")
PIP_BUILD_APT = ["build-essential", "python3-dev", "pkg-config"]
PIP_APT_DEPS = {
    "psycopg2": ["libpq-dev"],
    "psycopg": ["libpq-dev"],
    "pillow": ["libjpeg-dev", "zlib1g-dev", "libpng-dev", "libtiff-dev", "libfreetype6-dev"],
    "cryptography": ["libssl-dev", "libffi-dev"],
    "lxml": ["libxml2-dev", "libxslt1-dev"],
    "mysqlclient": ["default-libmysqlclient-dev"],
    "opencv-python": ["libgl1", "libglib2.0-0"],
    "opencv-contrib-python": ["libgl1", "libglib2.0-0"],
    "opencv-python-headless": ["libglib2.0-0"],
    "pyaudio": ["portaudio19-dev"],
    "python-ldap": ["libldap2-dev", "libsasl2-dev"],
    "cffi": ["libffi-dev"],
    "scipy": ["gfortran", "libopenblas-dev"],
    "matplotlib": ["libfreetype6-dev"],
    "pygame": ["libsdl2-dev", "libsdl2-image-dev", "libsdl2-mixer-dev", "libsdl2-ttf-dev"],
    "pyodbc": ["unixodbc-dev"],
    "reportlab": ["libfreetype6-dev"],
    "pycairo": ["libcairo2-dev"],
    "cairocffi": ["libcairo2-dev"],
    "netcdf4": ["libnetcdf-dev"],
    "h5py": ["libhdf5-dev"],
}


def parse_packages(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data]


def spec_to_dict(spec: Spec) -> dict:
    env = resolved_env(spec)
    return {
        "id": spec.id,
        "name": spec.name,
        "summary": spec.summary,
        "docker_image": spec.docker_image,
        "memory": spec.memory,
        "python_version": spec.python_version,
        "pip_packages": parse_packages(spec.pip_packages),
        "apt_packages": parse_packages(spec.apt_packages),
        "kind": spec.kind or "vscode",
        "env": env,
        "command": parse_command(getattr(spec, "command_json", None)),
        "access": service_access(spec.docker_image, spec.workspace_id, None, env)
        if (spec.kind or "vscode") == "container"
        else None,
        "notes": spec.notes,
        "markdown": resolved_markdown(spec),
        "workspace_id": spec.workspace_id,
        "created_at": spec.created_at.isoformat() if spec.created_at else None,
    }


def normalize_image(image: str) -> str:
    value = (image or "").strip().lower()
    if not value:
        raise ValueError("이미지 이름이 비어 있습니다.")
    if any(ch in value for ch in " \t\n;|&$`\\'\""):
        raise ValueError(f"허용되지 않는 이미지 표기입니다: {image}")
    if ":" not in value:
        value = f"{value}:latest"
    if IMAGE_PATTERN.fullmatch(value):
        return value
    raise ValueError(
        "이미지 형식이 올바르지 않습니다. 예: postgres:16, redis:7-alpine, python:3.12-slim, ubuntu:24.04"
    )


def normalize_kind(kind: str | None, image: str | None = None) -> str:
    value = (kind or "").strip().lower()
    aliases = {
        "vscode": "vscode",
        "ide": "vscode",
        "code": "vscode",
        "code-server": "vscode",
        "container": "container",
        "plain": "container",
        "service": "container",
        "db": "container",
    }
    if value in aliases:
        return aliases[value]
    if value:
        raise ValueError("종류는 vscode 또는 container 여야 합니다.")
    return "container" if is_service_image(image or "") else "vscode"


def normalize_memory(memory: str) -> str:
    value = (memory or "").strip().lower()
    if value in ALLOWED_MEMORY:
        return value
    raise ValueError("메모리는 512m, 1g, 2g, 4g, 8g 중 하나여야 합니다.")


def normalize_packages(packages: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for item in packages or []:
        pkg = item.strip()
        if not pkg:
            continue
        if not PIP_PATTERN.match(pkg) or any(ch in pkg for ch in ";|&$`\n"):
            raise ValueError(f"허용되지 않는 pip 패키지 표기입니다: {pkg}")
        cleaned.append(pkg)
    return cleaned


def normalize_apt_packages(packages: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for item in packages or []:
        pkg = item.strip().lower()
        if not pkg:
            continue
        if not APT_PATTERN.match(pkg) or any(ch in pkg for ch in ";|&$`\n"):
            raise ValueError(f"허용되지 않는 apt 패키지 표기입니다: {pkg}")
        cleaned.append(pkg)
    return cleaned


def python_version_from_image(image: str, fallback: str = "3.12") -> str:
    match = re.search(r"python:(\d+\.\d+)", image)
    return match.group(1) if match else fallback


def pip_dist_name(pkg: str) -> str:
    name = re.split(r"[<>=!~\[]", pkg, maxsplit=1)[0].strip().lower()
    return name.replace("_", "-")


def apt_packages_for_pip(pip_packages: list[str] | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    needs_build = False
    for pkg in pip_packages or []:
        deps = PIP_APT_DEPS.get(pip_dist_name(pkg))
        if deps is None:
            continue
        if deps:
            needs_build = True
        for dep in deps:
            if dep not in seen:
                seen.add(dep)
                found.append(dep)
    if needs_build:
        for extra in PIP_BUILD_APT:
            if extra not in seen:
                seen.add(extra)
                found.append(extra)
    return found


def merge_apt_packages(explicit: list[str] | None, pip_packages: list[str] | None) -> list[str]:
    merged = list(explicit or [])
    seen = set(merged)
    for extra in apt_packages_for_pip(pip_packages):
        if extra not in seen:
            seen.add(extra)
            merged.append(extra)
    return merged


def image_basename(image: str) -> str:
    return (image or "").split(":")[0].split("/")[-1]


def is_service_image(image: str) -> bool:
    return image_basename(image) in {
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
        "redis",
        "mongo",
        "mongodb",
        "rabbitmq",
        "memcached",
        "elasticsearch",
        "minio",
        "neo4j",
        "clickhouse",
        "cassandra",
        "influxdb",
    }


# bash/sh만 있고 데몬이 없어서 바로 종료되는 OS/언어 이미지
KEEP_ALIVE_IMAGES = {
    "ubuntu",
    "debian",
    "rockylinux",
    "rocky",
    "almalinux",
    "centos",
    "fedora",
    "alpine",
    "amazonlinux",
    "oraclelinux",
    "opensuse",
    "python",
    "node",
    "golang",
    "gcc",
}


def needs_keep_alive(image: str) -> bool:
    if is_service_image(image):
        return False
    return image_basename(image) in KEEP_ALIVE_IMAGES


def service_volume_path(image: str) -> str:
    return {
        "postgres": "/var/lib/postgresql/data",
        "postgresql": "/var/lib/postgresql/data",
        "mysql": "/var/lib/mysql",
        "mariadb": "/var/lib/mysql",
        "redis": "/data",
        "mongo": "/data/db",
        "mongodb": "/data/db",
    }.get(image_basename(image), "/data")


def parse_env(raw: str | dict | None) -> dict[str, str]:
    if isinstance(raw, dict):
        data = raw
    elif raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    out: dict[str, str] = {}
    for key, value in data.items():
        name = str(key).strip()
        text = str(value).strip()
        if name and text:
            out[name] = text
    return out


def parse_command(raw: str | list | None) -> list[str]:
    if isinstance(raw, list):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    elif raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = shlex.split(raw)
        if isinstance(data, list):
            parts = [str(item).strip() for item in data if str(item).strip()]
        elif isinstance(data, str) and data.strip():
            parts = shlex.split(data)
        else:
            parts = []
    else:
        parts = []
    cleaned: list[str] = []
    for part in parts:
        if any(ch in part for ch in ";|&$`\n"):
            raise ValueError(f"허용되지 않는 실행 인자입니다: {part}")
        cleaned.append(part)
    return cleaned


def default_service_env(image: str) -> dict[str, str]:
    name = image_basename(image)
    if name in {"postgres", "postgresql"}:
        return {
            "POSTGRES_PASSWORD": "tomato",
            "POSTGRES_USER": "tomato",
            "POSTGRES_DB": "tomato",
        }
    if name in {"mysql", "mariadb"}:
        return {
            "MYSQL_ROOT_PASSWORD": "tomato",
            "MYSQL_DATABASE": "tomato",
            "MYSQL_USER": "tomato",
            "MYSQL_PASSWORD": "tomato",
        }
    if name in {"mongo", "mongodb"}:
        return {
            "MONGO_INITDB_ROOT_USERNAME": "tomato",
            "MONGO_INITDB_ROOT_PASSWORD": "tomato",
        }
    return {}


def resolved_env(spec: Spec | None = None, *, image: str | None = None, extra: dict | None = None) -> dict[str, str]:
    docker_image = image or (spec.docker_image if spec is not None else "")
    stored = parse_env(getattr(spec, "env_json", None) if spec is not None else None)
    merged = {**default_service_env(docker_image), **stored, **parse_env(extra)}
    return {key: value for key, value in merged.items() if value}


def resolved_command(spec: Spec | None = None, extra: list[str] | str | None = None) -> list[str]:
    stored = parse_command(getattr(spec, "command_json", None) if spec is not None else None)
    override = parse_command(extra)
    return override or stored


def run_config_changed(
    image: str,
    old_env: dict[str, str] | None,
    new_env: dict[str, str] | None,
    old_command: list[str] | None = None,
    new_command: list[str] | None = None,
) -> tuple[bool, bool]:
    env_changed = service_environment(image, old_env) != service_environment(image, new_env)
    command_changed = parse_command(old_command) != parse_command(new_command)
    return env_changed or command_changed, env_changed


def format_run_command(
    image: str,
    *,
    env: dict[str, str] | None = None,
    command: list[str] | None = None,
    workspace_id: str | None = None,
) -> str:
    name = f"tomato-ws-{workspace_id}" if workspace_id else "tomato-ws-<id>"
    parts = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--hostname",
        name,
        "--network",
        "tomato-studio",
    ]
    for key, value in (env or {}).items():
        parts.extend(["-e", f"{key}={value}"])
    parts.append(image)
    parts.extend(command or [])
    return shlex.join(parts)


def service_environment(image: str, env: dict[str, str] | None = None) -> dict[str, str]:
    return {**default_service_env(image), **parse_env(env)}


def service_access(
    image: str,
    workspace_id: str | None,
    slug: str | None = None,
    env: dict[str, str] | None = None,
) -> dict | None:
    hostname = f"tomato-ws-{workspace_id}" if workspace_id else "tomato-ws-<id>"
    aliases = [hostname]
    if slug:
        aliases.append(slug)
    name = image_basename(image)
    resolved = service_environment(image, env)
    common = {
        "network": "tomato-studio",
        "hostname": hostname,
        "aliases": aliases,
    }
    if name in {"postgres", "postgresql"}:
        return {
            **common,
            "port": 5432,
            "user": resolved.get("POSTGRES_USER", "tomato"),
            "password": resolved.get("POSTGRES_PASSWORD", "tomato"),
            "database": resolved.get("POSTGRES_DB", "tomato"),
        }
    if name in {"mysql", "mariadb"}:
        return {
            **common,
            "port": 3306,
            "user": resolved.get("MYSQL_USER", "tomato"),
            "password": resolved.get("MYSQL_PASSWORD", "tomato"),
            "database": resolved.get("MYSQL_DATABASE", "tomato"),
        }
    if name == "redis":
        return {**common, "port": 6379}
    if name in {"mongo", "mongodb"}:
        return {
            **common,
            "port": 27017,
            "user": resolved.get("MONGO_INITDB_ROOT_USERNAME", "tomato"),
            "password": resolved.get("MONGO_INITDB_ROOT_PASSWORD", "tomato"),
        }
    if name == "memcached":
        return {**common, "port": 11211}
    if name == "rabbitmq":
        return {**common, "port": 5672, "user": "guest", "password": "guest"}
    return common


SPEC_MARKDOWN_MAX = 6000
SPEC_MARKDOWN_GUIDE = """마크다운 사양서 템플릿 (이 절 제목만 사용, HTML 금지, 4000자 이내):

# {이름}

## 개요
한두 문장으로 무엇을 위한 환경인지.

## 실행
- 종류: VS Code 또는 일반 컨테이너
- 이미지: `이름:태그`
- 메모리: 512m | 1g | 2g | 4g | 8g
- 실행 명령: 부가 요청이 있으면 실제 쓸 `docker run ...` 한 줄. 없으면 생략.

## 설치
pip/apt가 있을 때만. 없으면 절 생략.
- pip: `패키지`
- apt: `패키지`

## 접속
다른 컨테이너에서 붙어야 할 때만. 없으면 절 생략.
- 네트워크: tomato-studio
- 호스트: tomato-ws-<id>
- 포트/계정/비밀번호는 알면 적기

## 메모
환경변수, 실행 방식, 주의점. 2~6줄. 없으면 생략.

제목은 개요/실행/설치/접속/메모만. 실행 절의 종류·이미지·메모리는 구조화 필드와 같아야 함.
"""


def normalize_markdown(raw: str | None) -> str:
    text = (raw or "").replace("\r\n", "\n").strip()
    text = re.sub(r"<[^>]+>", "", text)
    if len(text) > SPEC_MARKDOWN_MAX:
        text = text[:SPEC_MARKDOWN_MAX].rstrip()
    return text


def build_spec_markdown(
    *,
    name: str,
    summary: str,
    docker_image: str,
    memory: str,
    kind: str,
    pip_packages: list[str] | None = None,
    apt_packages: list[str] | None = None,
    notes: str = "",
    workspace_id: str | None = None,
    env: dict[str, str] | None = None,
    command: list[str] | None = None,
) -> str:
    lines = [
        f"# {name}",
        "",
        "## 개요",
        summary.strip() or "요청한 개발 환경",
        "",
        "## 실행",
        *execution_body(
            docker_image=docker_image,
            memory=memory,
            kind=kind,
            env=env,
            command=command,
            workspace_id=workspace_id,
        ),
    ]
    pip_packages = pip_packages or []
    apt_packages = apt_packages or []
    if kind == "vscode" and (pip_packages or apt_packages):
        lines += ["", "## 설치"]
        if pip_packages:
            lines.append("- pip: " + ", ".join(f"`{item}`" for item in pip_packages))
        if apt_packages:
            lines.append("- apt: " + ", ".join(f"`{item}`" for item in apt_packages))
    if kind == "container":
        lines += ["", "## 접속", *access_body(service_access(docker_image, workspace_id, env=env))]
    if (notes or "").strip():
        lines += ["", "## 메모", notes.strip()]
    return "\n".join(lines)


def replace_section(text: str, heading: str, body_lines: list[str]) -> str:
    block = "\n".join([f"## {heading}", *body_lines])
    if re.search(rf"^## {re.escape(heading)}\s*$", text, flags=re.M):
        return re.sub(
            rf"^## {re.escape(heading)}\s*\n(?:.*\n)*?(?=^## |\Z)",
            block + "\n\n",
            text,
            count=1,
            flags=re.M,
        )
    return text.rstrip() + "\n\n" + block + "\n"


def execution_body(
    *,
    docker_image: str,
    memory: str,
    kind: str,
    env: dict[str, str] | None = None,
    command: list[str] | None = None,
    workspace_id: str | None = None,
) -> list[str]:
    kind_label = "VS Code" if kind == "vscode" else "일반 컨테이너"
    lines = [
        f"- 종류: {kind_label}",
        f"- 이미지: `{docker_image}`",
        f"- 메모리: {memory}",
    ]
    if kind == "container":
        lines.append(
            "- 실행 명령: `"
            + format_run_command(docker_image, env=env, command=command, workspace_id=workspace_id)
            + "`"
        )
    return lines


def access_body(access: dict | None) -> list[str]:
    if not access:
        return ["- 네트워크: `tomato-studio`"]
    lines = [f"- 네트워크: `{access.get('network', 'tomato-studio')}`"]
    if access.get("hostname"):
        lines.append(f"- 호스트: `{access['hostname']}`")
    if access.get("port"):
        lines.append(f"- 포트: {access['port']}")
    if access.get("user"):
        lines.append(f"- 계정: `{access['user']}`")
    if access.get("password"):
        lines.append(f"- 비밀번호: `{access['password']}`")
    if access.get("database"):
        lines.append(f"- DB: `{access['database']}`")
    return lines


def resolved_markdown(spec: Spec) -> str:
    env = resolved_env(spec)
    command = resolved_command(spec)
    text = (getattr(spec, "markdown", None) or "").strip()
    if not text:
        text = build_spec_markdown(
            name=spec.name,
            summary=spec.summary,
            docker_image=spec.docker_image,
            memory=spec.memory,
            kind=spec.kind or "vscode",
            pip_packages=parse_packages(spec.pip_packages),
            apt_packages=parse_packages(spec.apt_packages),
            notes=spec.notes or "",
            workspace_id=spec.workspace_id,
            env=env,
            command=command,
        )
    else:
        text = replace_section(
            text,
            "실행",
            execution_body(
                docker_image=spec.docker_image,
                memory=spec.memory,
                kind=spec.kind or "vscode",
                env=env,
                command=command,
                workspace_id=spec.workspace_id,
            ),
        )
        if (spec.kind or "vscode") == "container":
            text = replace_section(
                text,
                "접속",
                access_body(service_access(spec.docker_image, spec.workspace_id, env=env)),
            )
    if spec.workspace_id:
        text = text.replace("tomato-ws-<id>", f"tomato-ws-{spec.workspace_id}")
    return text


def write_spec(
    db: Session,
    *,
    name: str,
    summary: str,
    docker_image: str,
    memory: str,
    pip_packages: list[str] | None = None,
    apt_packages: list[str] | None = None,
    python_version: str | None = None,
    kind: str | None = None,
    notes: str = "",
    markdown: str | None = None,
    spec_id: str | None = None,
    env: dict | None = None,
    command: list[str] | str | None = None,
) -> Spec:
    image = normalize_image(docker_image)
    resolved_kind = normalize_kind(kind, image)
    if resolved_kind == "vscode":
        pip = normalize_packages(pip_packages)
        apt = merge_apt_packages(normalize_apt_packages(apt_packages), pip)
    else:
        pip = []
        apt = []
    note_text = (notes or "").strip()
    body = normalize_markdown(markdown)
    existing = db.get(Spec, spec_id) if spec_id else None
    merged_env = resolved_env(existing, image=image, extra=env)
    merged_command = resolved_command(existing, extra=command)
    mem = normalize_memory(memory)
    workspace_id = existing.workspace_id if existing is not None else None
    if not body:
        body = build_spec_markdown(
            name=name.strip()[:80] or "dev-env",
            summary=summary,
            docker_image=image,
            memory=mem,
            kind=resolved_kind,
            pip_packages=pip,
            apt_packages=apt,
            notes=note_text,
            workspace_id=workspace_id,
            env=merged_env,
            command=merged_command,
        )
    elif resolved_kind == "container":
        body = replace_section(
            body,
            "실행",
            execution_body(
                docker_image=image,
                memory=mem,
                kind=resolved_kind,
                env=merged_env,
                command=merged_command,
                workspace_id=workspace_id,
            ),
        )
        body = replace_section(
            body,
            "접속",
            access_body(service_access(image, workspace_id, env=merged_env)),
        )
    env_text = json.dumps(merged_env, ensure_ascii=False)
    command_text = json.dumps(merged_command, ensure_ascii=False)
    if existing is not None:
        existing.name = name.strip()[:80] or existing.name
        existing.summary = (summary or "").strip()
        existing.docker_image = image
        existing.memory = mem
        existing.python_version = (python_version or python_version_from_image(image)).strip()[:16]
        existing.pip_packages = json.dumps(pip, ensure_ascii=False)
        existing.apt_packages = json.dumps(apt, ensure_ascii=False)
        existing.kind = resolved_kind
        existing.notes = note_text
        existing.markdown = body
        existing.env_json = env_text
        existing.command_json = command_text
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing
    spec = Spec(
        id=new_id(),
        name=name.strip()[:80] or "dev-env",
        summary=(summary or "").strip(),
        docker_image=image,
        memory=mem,
        python_version=(python_version or python_version_from_image(image)).strip()[:16],
        pip_packages=json.dumps(pip, ensure_ascii=False),
        apt_packages=json.dumps(apt, ensure_ascii=False),
        kind=resolved_kind,
        notes=note_text,
        markdown=body,
        env_json=env_text,
        command_json=command_text,
    )
    db.add(spec)
    db.commit()
    db.refresh(spec)
    return spec


def get_spec(db: Session, spec_id: str) -> Spec | None:
    return db.get(Spec, spec_id)


def list_specs(db: Session) -> list[Spec]:
    return db.query(Spec).order_by(Spec.created_at.desc()).all()


def touch_spec_workspace(db: Session, spec: Spec, workspace_id: str | None) -> None:
    spec.workspace_id = workspace_id
    spec.created_at = spec.created_at or utcnow()
    db.add(spec)
    db.commit()
