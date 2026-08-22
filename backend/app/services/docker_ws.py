from __future__ import annotations

import io
import json
import re
import socket
import tarfile
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import docker
import httpx
import paramiko
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.transport.sshconn import SSHConnection, SSHHTTPAdapter
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Workspace, utcnow

LABEL_STUDIO = "tomato.studio"
LABEL_WORKSPACE_ID = "tomato.workspace.id"
NETWORK_NAME = "tomato-studio"
PROJECT_DIR = "/home/coder/project"

_client_lock = threading.Lock()
_docker_client = None
_workspace_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_global_lock = threading.Lock()
_progress_lock = threading.Lock()
_progress: dict[str, list[str]] = {}


def add_progress(workspace_id: str, message: str) -> None:
    line = (message or "").strip()
    if not line:
        return
    with _progress_lock:
        rows = _progress.setdefault(workspace_id, [])
        rows.append(line[:400])
        _progress[workspace_id] = rows[-80:]


def get_progress(workspace_id: str) -> list[str]:
    with _progress_lock:
        return list(_progress.get(workspace_id, []))


def clear_progress(workspace_id: str) -> None:
    with _progress_lock:
        _progress.pop(workspace_id, None)


def remote_docker_hostname() -> str | None:
    host = (settings.docker_host or "").strip()
    if not host:
        return None
    parsed = urlparse(host)
    if parsed.scheme in {"ssh", "tcp", "http", "https"} and parsed.hostname:
        return parsed.hostname
    return None


def resolved_public_workspace_host() -> str:
    configured = (settings.public_workspace_host or "").strip()
    remote = remote_docker_hostname()
    if remote and configured in {"", "localhost", "127.0.0.1"}:
        return remote
    return configured or "localhost"


def resolved_workspace_bind_ip() -> str:
    configured = (settings.workspace_bind_ip or "").strip() or "127.0.0.1"
    if remote_docker_hostname() and configured in {"127.0.0.1", "localhost"}:
        return "0.0.0.0"
    return configured


def _ssh_password() -> str:
    parsed = urlparse((settings.docker_host or "").strip())
    return (settings.docker_ssh_password or parsed.password or "").strip()


def _ssh_base_url(host: str) -> str:
    parsed = urlparse(host)
    user = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{user}@" if user else ""
    return f"ssh://{auth}{hostname}{port}"


_ssh_open_lock = threading.Lock()


def _enable_ssh_password_login() -> None:
    original_create = SSHHTTPAdapter._create_paramiko_client
    original_connect = SSHConnection.connect
    original_close = SSHConnection.close

    def _create_paramiko_client(self, base_url):
        original_create(self, base_url)
        password = _ssh_password()
        if password:
            self.ssh_params["password"] = password
            self.ssh_params["look_for_keys"] = False
            self.ssh_params["allow_agent"] = False
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def _connect(self):
        password = _ssh_password()
        last_error: Exception | None = None
        for attempt in range(6):
            try:
                transport = self.ssh_transport
                if transport is None or not transport.is_active():
                    raise paramiko.SSHException("SSH transport is closed")
                with _ssh_open_lock:
                    sock = transport.open_session()
                sock.settimeout(self.timeout)
                if password:
                    sock.exec_command(
                        "sudo -S -p '' /var/packages/ContainerManager/target/usr/bin/docker system dial-stdio"
                    )
                    sock.send((password + "\n").encode("utf-8"))
                else:
                    sock.exec_command("docker system dial-stdio")
                self.sock = sock
                return
            except paramiko.ssh_exception.ChannelException as exc:
                last_error = exc
                time.sleep(0.6 * (attempt + 1))
                continue
            except Exception as exc:
                last_error = exc
                time.sleep(0.4 * (attempt + 1))
        if last_error:
            raise last_error
        return original_connect(self)

    def _close(self):
        sock = getattr(self, "sock", None)
        try:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            original_close(self)
        finally:
            self.sock = None

    SSHHTTPAdapter._create_paramiko_client = _create_paramiko_client
    SSHConnection.connect = _connect
    SSHConnection.close = _close


_enable_ssh_password_login()


def reset_docker_client() -> None:
    global _docker_client
    with _client_lock:
        client = _docker_client
        _docker_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def _make_docker_client():
    host = (settings.docker_host or "").strip()
    if not host:
        return docker.from_env()
    if host.startswith("ssh://"):
        use_password = bool(_ssh_password())
        return docker.DockerClient(
            base_url=_ssh_base_url(host),
            use_ssh_client=not use_password,
            timeout=600,
            max_pool_size=1,
        )
    return docker.DockerClient(base_url=host, timeout=60, max_pool_size=1)


def docker_client():
    global _docker_client
    with _client_lock:
        if _docker_client is None:
            _docker_client = _make_docker_client()
        return _docker_client


def _retry_ssh(operation):
    try:
        return operation()
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        if "Connect failed" not in text and "ChannelException" not in text:
            raise
        reset_docker_client()
        time.sleep(1)
        return operation()


def workspace_lock(workspace_id: str) -> threading.Lock:
    with _locks_guard:
        if workspace_id not in _workspace_locks:
            _workspace_locks[workspace_id] = threading.Lock()
        return _workspace_locks[workspace_id]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)[:80]
    slug = base
    index = 2
    while db.query(Workspace).filter(Workspace.slug == slug).first():
        slug = f"{base}-{index}"
        index += 1
    return slug


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def public_url(port: int | None) -> str | None:
    if not port:
        return None
    return f"http://{resolved_public_workspace_host()}:{port}"


def ensure_network() -> None:
    client = docker_client()
    try:
        client.networks.get(NETWORK_NAME)
    except NotFound:
        client.networks.create(NETWORK_NAME, check_duplicate=True)


def ensure_image(workspace_id: str | None = None, image: str | None = None) -> None:
    target = (image or settings.docker_image).strip()
    client = docker_client()
    try:
        client.images.get(target)
        if workspace_id:
            add_progress(workspace_id, f"이미지 확인: {target}")
        return
    except ImageNotFound:
        pass
    if workspace_id:
        add_progress(workspace_id, f"이미지 다운로드: {target}")
    client.images.pull(target)
    if workspace_id:
        add_progress(workspace_id, "이미지 다운로드 완료")


def create_workspace(
    db: Session,
    name: str,
    *,
    spec_id: str | None = None,
    memory_limit: str | None = None,
    pip_packages: list[str] | None = None,
    apt_packages: list[str] | None = None,
    docker_image: str | None = None,
    kind: str = "vscode",
) -> Workspace:
    from .volume_files import ensure_writable

    workspace_id = new_id()
    volume_name = f"tomato-ws-{workspace_id}"
    resolved_kind = (kind or "vscode").strip().lower() or "vscode"

    def _prepare_volume():
        add_progress(workspace_id, f"볼륨 생성: {volume_name}")
        client = docker_client()
        try:
            client.volumes.create(volume_name, labels={LABEL_STUDIO: "workspace"})
        except APIError:
            pass
        if resolved_kind == "vscode":
            add_progress(workspace_id, "볼륨 권한 설정")
            ensure_writable(volume_name, vscode_defaults=True)
        add_progress(workspace_id, "볼륨 준비 완료")

    _retry_ssh(_prepare_volume)
    workspace = Workspace(
        id=workspace_id,
        name=name.strip(),
        slug=unique_slug(db, name),
        volume_name=volume_name,
        status="stopped",
        spec_id=spec_id,
        memory_limit=memory_limit or settings.mem_limit,
        pip_packages=json.dumps(pip_packages or [], ensure_ascii=False),
        apt_packages=json.dumps(apt_packages or [], ensure_ascii=False),
        docker_image=docker_image,
        kind=resolved_kind,
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


def _container_status(container) -> str:
    state = (container.attrs.get("State") or {}).get("Status", "")
    if state == "running":
        return "running"
    if state in {"created", "paused", "restarting"}:
        return "starting"
    return "stopped"


def _host_port(container) -> int | None:
    ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    bindings = ports.get("8080/tcp") or []
    if not bindings:
        return None
    raw = bindings[0].get("HostPort")
    return int(raw) if raw else None


def _get_container(workspace: Workspace):
    client = docker_client()
    if workspace.container_id:
        try:
            container = client.containers.get(workspace.container_id)
            container.reload()
            return container
        except NotFound:
            workspace.container_id = None
    name = f"tomato-ws-{workspace.id}"
    try:
        container = client.containers.get(name)
        container.reload()
        workspace.container_id = container.id
        return container
    except NotFound:
        return None


def sync_from_docker(workspace: Workspace) -> Workspace:
    try:
        container = _get_container(workspace)
    except DockerException:
        return workspace
    if container is None:
        if workspace.status in {"running", "stopping"}:
            workspace.status = "stopped"
        workspace.host_port = None
        return workspace
    workspace.container_id = container.id
    workspace.host_port = _host_port(container)
    live = _container_status(container)
    if workspace.status == "starting":
        return workspace
    if workspace.status == "stopping" and live == "running":
        return workspace
    if workspace.status != "error":
        workspace.status = live
    return workspace


def list_running(db: Session) -> list[Workspace]:
    workspaces = db.query(Workspace).all()
    running: list[Workspace] = []
    for workspace in workspaces:
        sync_from_docker(workspace)
        if workspace.status == "running":
            running.append(workspace)
    db.commit()
    return running


def _stop_container(workspace: Workspace) -> None:
    container = _get_container(workspace)
    if container is None:
        workspace.status = "stopped"
        workspace.host_port = None
        return
    try:
        if _container_status(container) == "running":
            container.stop(timeout=10)
    except DockerException:
        pass
    workspace.status = "stopped"
    workspace.host_port = None


def enforce_running_limit(db: Session, keep_id: str) -> None:
    running = [
        workspace
        for workspace in list_running(db)
        if workspace.id != keep_id and _is_vscode(workspace)
    ]
    running.sort(key=lambda item: item.last_accessed_at)
    overflow = len(running) + 1 - settings.max_running
    for workspace in running[: max(overflow, 0)]:
        _stop_container(workspace)
        db.add(workspace)
    db.commit()


def _wait_until_ready(port: int) -> None:
    deadline = time.time() + settings.ready_timeout_seconds
    url = f"http://{resolved_public_workspace_host()}:{port}/"
    with httpx.Client(follow_redirects=True, timeout=2.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(url)
                if response.status_code < 500:
                    return
            except httpx.HTTPError:
                time.sleep(0.6)
    raise TimeoutError("code-server가 준비되지 않았습니다.")


def _json_package_list(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _package_list(workspace: Workspace) -> list[str]:
    return _json_package_list(workspace.pip_packages)


def _apt_package_list(workspace: Workspace) -> list[str]:
    packages: list[str] = []
    for item in _json_package_list(workspace.apt_packages):
        pkg = item.lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?:=[A-Za-z0-9.+~:-]+)?", pkg):
            packages.append(pkg)
    return packages


def _wait_container_running(container, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        container.reload()
        if _container_status(container) == "running":
            return
        state = ((container.attrs or {}).get("State") or {}).get("Status", "")
        if state in {"exited", "dead"}:
            raise RuntimeError("컨테이너가 바로 종료되었습니다.")
        time.sleep(0.5)
    raise RuntimeError("컨테이너가 실행 중 상태가 되지 않았습니다.")


def _put_text_in_container(container, relpath: str, content: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        data = content.encode("utf-8")
        info = tarfile.TarInfo(name=relpath)
        info.size = len(data)
        info.uid = 1000
        info.gid = 1000
        info.mode = 0o664
        tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    container.put_archive(PROJECT_DIR, buffer.getvalue())


def _exec_logged(workspace_id: str, container, command: str, *, user: str = "root") -> str:
    add_progress(workspace_id, command if len(command) < 180 else command[:177] + "...")
    client = docker_client()
    exec_id = client.api.exec_create(container.id, ["bash", "-lc", command], user=user, stdout=True, stderr=True)
    output = b""
    for chunk in client.api.exec_start(exec_id, stream=True):
        if not chunk:
            continue
        output += chunk if isinstance(chunk, (bytes, bytearray)) else b""
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                add_progress(workspace_id, stripped)
    inspect = client.api.exec_inspect(exec_id)
    decoded = output.decode("utf-8", errors="replace")
    code = inspect.get("ExitCode") or 0
    if code != 0:
        raise RuntimeError(decoded[-3000:] or f"명령이 실패했습니다 (exit {code})")
    return decoded


def _marker_matches(container, filename: str, marker: str) -> bool:
    already = container.exec_run(
        [
            "bash",
            "-lc",
            (
                f"test -f {PROJECT_DIR}/{filename} && "
                f"printf '%s\\n' {json.dumps(marker)} | cmp -s - {PROJECT_DIR}/{filename}"
            ),
        ]
    )
    return already.exit_code == 0


def _prepare_python_env(workspace: Workspace, container) -> None:
    from .specs import PIP_BUILD_APT, apt_packages_for_pip

    pip_packages = _package_list(workspace)
    apt_packages = _apt_package_list(workspace)
    for extra in apt_packages_for_pip(pip_packages):
        if extra not in apt_packages:
            apt_packages.append(extra)
    requirements = "\n".join(pip_packages) + "\n" if pip_packages else "# no extra packages\n"
    add_progress(workspace.id, "requirements.txt 작성 중")
    _wait_container_running(container)
    _put_text_in_container(container, "requirements.txt", requirements)

    if not pip_packages and not apt_packages:
        add_progress(workspace.id, "추가 pip/apt 패키지 없음")
        _exec_logged(workspace.id, container, f"chown -R 1000:1000 {PROJECT_DIR}")
        return

    apt_to_install = list(apt_packages)
    if pip_packages:
        for extra in ("python3", "python3-pip"):
            if extra not in apt_to_install:
                apt_to_install.append(extra)

    apt_marker = ",".join(sorted(apt_to_install))
    pip_marker = ",".join(sorted(pip_packages))
    need_apt = bool(apt_to_install) and not _marker_matches(container, ".tomato-apt-ready", apt_marker)
    need_pip = bool(pip_packages) and not _marker_matches(container, ".tomato-python-ready", pip_marker)

    def _apt_install(packages: list[str], marker: str) -> None:
        quoted = " ".join(packages)
        add_progress(workspace.id, f"apt 패키지 설치: {quoted}")
        _exec_logged(
            workspace.id,
            container,
            (
                "set -e; export DEBIAN_FRONTEND=noninteractive; "
                "apt-get update; "
                f"apt-get install -y {quoted}; "
                f"printf '%s\\n' {json.dumps(marker)} > {PROJECT_DIR}/.tomato-apt-ready"
            ),
        )
        add_progress(workspace.id, "apt 설치 완료")

    def _pip_install() -> None:
        add_progress(workspace.id, "pip install 시작")
        _exec_logged(
            workspace.id,
            container,
            (
                "set -e; "
                f"python3 -m pip install --break-system-packages -r {PROJECT_DIR}/requirements.txt; "
                f"printf '%s\\n' {json.dumps(pip_marker)} > {PROJECT_DIR}/.tomato-python-ready"
            ),
        )
        add_progress(workspace.id, "pip 설치 완료")

    if need_apt:
        _apt_install(apt_to_install, apt_marker)
    elif apt_to_install:
        add_progress(workspace.id, "이미 설치된 apt 패키지라 건너뜁니다")

    if need_pip:
        try:
            _pip_install()
        except RuntimeError:
            extra_build = [item for item in PIP_BUILD_APT if item not in apt_to_install]
            if not extra_build:
                raise
            add_progress(workspace.id, "pip 빌드 의존성 부족, 컴파일 도구 설치 후 재시도")
            apt_to_install.extend(extra_build)
            apt_marker = ",".join(sorted(apt_to_install))
            _apt_install(apt_to_install, apt_marker)
            _pip_install()
    elif pip_packages:
        add_progress(workspace.id, "이미 설치된 pip 패키지라 건너뜁니다")

    _exec_logged(workspace.id, container, f"chown -R 1000:1000 {PROJECT_DIR}")


def _workspace_kind(workspace: Workspace) -> str:
    return (workspace.kind or "vscode").strip().lower() or "vscode"


def _is_vscode(workspace: Workspace) -> bool:
    return _workspace_kind(workspace) == "vscode"


VSCODE_DARK_SETTINGS = {
    "workbench.colorTheme": "Default Dark Modern",
    "workbench.preferredDarkColorTheme": "Default Dark Modern",
    "window.autoDetectColorScheme": False,
}


def _ensure_vscode_dark_theme(container) -> None:
    """code-server User 설정이 없으면 다크 테마를 기본으로 넣는다.

    이미 colorTheme을 고른 워크스페이스는 건드리지 않는다.
    OS가 라이트 모드여도 prefers-color-scheme를 따라가지 않게 한다.
    """
    payload = json.dumps(VSCODE_DARK_SETTINGS)
    script = f"""
set -e
mkdir -p /home/coder/.local/share/code-server/User "{PROJECT_DIR}/.vscode"
node << 'EOF'
const fs = require('fs');
const defaults = {payload};
function apply(path) {{
  let data = {{}};
  if (fs.existsSync(path)) {{
    try {{
      const raw = JSON.parse(fs.readFileSync(path, 'utf8'));
      if (raw && typeof raw === 'object' && !Array.isArray(raw)) data = raw;
    }} catch (err) {{
      data = {{}};
    }}
  }}
  if (!data['workbench.colorTheme']) {{
    Object.assign(data, defaults);
    fs.writeFileSync(path, JSON.stringify(data, null, 2) + '\\n');
  }}
}}
apply('/home/coder/.local/share/code-server/User/settings.json');
apply('{PROJECT_DIR}/.vscode/settings.json');
EOF
chown -R 1000:1000 /home/coder/.local/share/code-server "{PROJECT_DIR}/.vscode"
"""
    try:
        container.exec_run(["sh", "-lc", script], user="root")
    except DockerException:
        pass


def _connect_aliases(container, aliases: list[str]) -> None:
    cleaned = [item for item in aliases if item]
    if not cleaned:
        return
    net = docker_client().networks.get(NETWORK_NAME)
    try:
        net.connect(container, aliases=cleaned)
        return
    except APIError:
        pass
    try:
        net.disconnect(container)
    except APIError:
        pass
    net.connect(container, aliases=cleaned)


def _run_vscode_container(workspace: Workspace):
    ensure_image(workspace.id)
    client = docker_client()
    trusted = settings.cors_origins or ["http://localhost:5173"]
    command = [
        "--auth",
        "none",
        "--bind-addr",
        "0.0.0.0:8080",
        "--disable-telemetry",
        "--disable-workspace-trust",
        "--disable-getting-started-override",
        "--cookie-suffix",
        workspace.id,
        "--app-name",
        "Tomato Studio",
        PROJECT_DIR,
    ]
    for origin in trusted:
        command.extend(["--trusted-origins", origin])

    add_progress(workspace.id, f"컨테이너 시작: tomato-ws-{workspace.id}")
    created = client.containers.run(
        settings.docker_image,
        command=command,
        name=f"tomato-ws-{workspace.id}",
        detach=True,
        network=NETWORK_NAME,
        mem_limit=workspace.memory_limit or settings.mem_limit,
        ports={"8080/tcp": (resolved_workspace_bind_ip(),)},
        volumes={workspace.volume_name: {"bind": PROJECT_DIR, "mode": "rw"}},
        labels={
            LABEL_STUDIO: "workspace",
            LABEL_WORKSPACE_ID: workspace.id,
            "tomato.kind": "vscode",
        },
        restart_policy={"Name": "no"},
    )
    _connect_aliases(created, [workspace.slug, workspace.id])
    _ensure_vscode_dark_theme(created)
    add_progress(workspace.id, "컨테이너가 생성되었습니다")
    return created


def _run_service_container(workspace: Workspace, *, keep_alive: bool | None = None):
    from .specs import needs_keep_alive, service_environment, service_volume_path

    image = (workspace.docker_image or "").strip() or "ubuntu:24.04"
    ensure_image(workspace.id, image)
    client = docker_client()
    env = service_environment(image)
    volume_path = service_volume_path(image)
    stay_up = needs_keep_alive(image) if keep_alive is None else keep_alive
    add_progress(workspace.id, f"컨테이너 시작: tomato-ws-{workspace.id} ({image})")
    run_kwargs = {
        "name": f"tomato-ws-{workspace.id}",
        "hostname": (workspace.slug or workspace.id)[:63],
        "detach": True,
        "network": NETWORK_NAME,
        "mem_limit": workspace.memory_limit or settings.mem_limit,
        "environment": env or None,
        "volumes": {workspace.volume_name: {"bind": volume_path, "mode": "rw"}},
        "labels": {
            LABEL_STUDIO: "workspace",
            LABEL_WORKSPACE_ID: workspace.id,
            "tomato.kind": "container",
        },
        "restart_policy": {"Name": "no"},
    }
    if stay_up:
        add_progress(workspace.id, "데몬이 없는 이미지라 대기 프로세스로 유지합니다")
        run_kwargs["command"] = ["/bin/sh", "-c", "sleep infinity || tail -f /dev/null"]
    created = client.containers.run(image, **run_kwargs)
    _connect_aliases(created, [workspace.slug, workspace.id])
    add_progress(workspace.id, f"네트워크 호스트: tomato-ws-{workspace.id}")
    if workspace.slug:
        add_progress(workspace.id, f"별칭: {workspace.slug}")
    add_progress(workspace.id, "컨테이너가 생성되었습니다")
    return created


def _run_container(workspace: Workspace):
    add_progress(workspace.id, "네트워크/이미지 확인")
    ensure_network()
    if _is_vscode(workspace):
        return _run_vscode_container(workspace)
    return _run_service_container(workspace)


def start_workspace(db: Session, workspace: Workspace, *, prepare_python: bool = True) -> Workspace:
    with workspace_lock(workspace.id):
        db.refresh(workspace)
        sync_from_docker(workspace)
        vscode = _is_vscode(workspace)
        if workspace.status == "running" and (not vscode or workspace.host_port):
            if vscode:
                container = _get_container(workspace)
                if container is not None:
                    _ensure_vscode_dark_theme(container)
                    if prepare_python:
                        _prepare_python_env(workspace, container)
            workspace.last_accessed_at = utcnow()
            workspace.error_message = None
            db.commit()
            db.refresh(workspace)
            return workspace

        workspace.status = "starting"
        workspace.error_message = None
        db.commit()
        add_progress(workspace.id, "워크스페이스 시작")
        try:
            def _boot():
                with _global_lock:
                    enforce_running_limit(db, workspace.id)
                container = _get_container(workspace)
                if container is None:
                    container = _run_container(workspace)
                elif _container_status(container) != "running":
                    container.start()
                container.reload()
                return container

            container = _retry_ssh(_boot)
            workspace.container_id = container.id
            if vscode:
                port = _host_port(container)
                if not port:
                    raise RuntimeError("호스트 포트를 할당하지 못했습니다.")
                add_progress(workspace.id, f"code-server 대기 중 (port {port})")
                _wait_until_ready(port)
                add_progress(workspace.id, "code-server 준비됨")
                _ensure_vscode_dark_theme(container)
                add_progress(workspace.id, "Python 환경 구성 시작")
                _prepare_python_env(workspace, container)
                add_progress(workspace.id, "준비 완료")
                workspace.host_port = port
            else:
                try:
                    _wait_container_running(container, timeout=12)
                except RuntimeError:
                    add_progress(workspace.id, "기본 명령이 바로 종료되어 대기 프로세스로 다시 시작합니다")
                    try:
                        container.remove(force=True)
                    except DockerException:
                        pass
                    workspace.container_id = None
                    container = _retry_ssh(lambda: _run_service_container(workspace, keep_alive=True))
                    workspace.container_id = container.id
                    _wait_container_running(container, timeout=20)
                add_progress(workspace.id, "서비스 컨테이너 실행 중")
                workspace.host_port = None
            workspace.status = "running"
            workspace.last_accessed_at = utcnow()
            db.commit()
            db.refresh(workspace)
            return workspace
        except Exception as exc:
            workspace.status = "error"
            workspace.error_message = str(exc)
            add_progress(workspace.id, f"실패: {exc}")
            db.commit()
            db.refresh(workspace)
            raise


def stop_workspace(db: Session, workspace: Workspace) -> Workspace:
    with workspace_lock(workspace.id):
        workspace.status = "stopping"
        workspace.error_message = None
        db.commit()
        db.refresh(workspace)

        workspace_id = workspace.id
        container_id = workspace.container_id

        def _do_stop():
            from ..db import SessionLocal
            bg_db = SessionLocal()
            try:
                ws = bg_db.get(Workspace, workspace_id)
                if ws is None:
                    return
                try:
                    container = _get_container(ws)
                    if container is not None and _container_status(container) == "running":
                        container.stop(timeout=10)
                except Exception:
                    pass
                ws.status = "stopped"
                ws.host_port = None
                bg_db.commit()
            except Exception:
                try:
                    ws = bg_db.get(Workspace, workspace_id)
                    if ws is not None:
                        ws.status = "stopped"
                        ws.host_port = None
                        bg_db.commit()
                except Exception:
                    pass
            finally:
                bg_db.close()

        threading.Thread(target=_do_stop, daemon=True).start()
        return workspace


def touch_workspace(db: Session, workspace: Workspace) -> Workspace:
    workspace.last_accessed_at = utcnow()
    db.commit()
    db.refresh(workspace)
    return workspace


def delete_workspace(db: Session, workspace: Workspace) -> None:
    with workspace_lock(workspace.id):
        container = _get_container(workspace)
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass
        try:
            docker_client().volumes.get(workspace.volume_name).remove(force=True)
        except (NotFound, DockerException):
            pass
        db.delete(workspace)
        db.commit()


def reap_idle(db: Session) -> None:
    now = datetime.now(timezone.utc)
    for workspace in db.query(Workspace).all():
        sync_from_docker(workspace)
        if workspace.status != "running":
            continue
        if not _is_vscode(workspace):
            continue
        last = workspace.last_accessed_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        idle_for = (now - last).total_seconds()
        if idle_for >= settings.idle_timeout_seconds:
            _stop_container(workspace)
    db.commit()


_SHELL_CMD = [
    "/bin/sh",
    "-c",
    (
        "if command -v bash >/dev/null 2>&1; then exec bash -il; "
        "elif command -v ash >/dev/null 2>&1; then exec ash -il; "
        "else exec sh -il; fi"
    ),
]


class ExecTty:
    def __init__(self, exec_id: str, sock, client):
        self.exec_id = exec_id
        self.sock = sock
        self.client = client
        self.closed = False
        self.lock = threading.Lock()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            closer = getattr(self.sock, "close", None)
            if callable(closer):
                closer()
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass


def _sock_settimeout(sock, timeout: float | None) -> None:
    for item in (sock, getattr(sock, "_sock", None), getattr(sock, "socket", None)):
        if item is None or not hasattr(item, "settimeout"):
            continue
        try:
            item.settimeout(timeout)
        except Exception:
            pass


def _sock_recv(sock, nbytes: int = 8192) -> bytes:
    recv = getattr(sock, "recv", None)
    if callable(recv):
        data = recv(nbytes)
        return data if isinstance(data, (bytes, bytearray)) else b""
    read = getattr(sock, "read", None)
    if callable(read):
        data = read(nbytes)
        return data if isinstance(data, (bytes, bytearray)) else b""
    raise RuntimeError("exec 소켓에서 읽을 수 없습니다.")


def _sock_send(sock, data: bytes) -> None:
    sendall = getattr(sock, "sendall", None)
    if callable(sendall):
        sendall(data)
        return
    view = memoryview(data)
    while view:
        send = getattr(sock, "send", None)
        if not callable(send):
            raise RuntimeError("exec 소켓에 쓸 수 없습니다.")
        sent = send(bytes(view[: min(len(view), 4096)]))
        if not sent:
            raise RuntimeError("exec 입력이 끊겼습니다.")
        view = view[sent:]


def open_container_exec(workspace: Workspace) -> ExecTty:
    container = _get_container(workspace)
    if container is None or _container_status(container) != "running":
        raise RuntimeError("실행 중인 컨테이너가 없습니다.")
    created = docker_client().api.exec_create(
        container.id,
        _SHELL_CMD,
        stdin=True,
        tty=True,
        stdout=True,
        stderr=True,
        environment={
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    exec_id = created["Id"]
    stream_client = _make_docker_client()
    try:
        sock = stream_client.api.exec_start(exec_id, tty=True, stream=True, socket=True)
        _sock_settimeout(sock, 0.5)
        return ExecTty(exec_id, sock, stream_client)
    except Exception:
        try:
            stream_client.close()
        except Exception:
            pass
        raise


def resize_exec(exec_id: str, cols: int, rows: int) -> None:
    cols = max(2, min(int(cols), 400))
    rows = max(2, min(int(rows), 200))
    docker_client().api.exec_resize(exec_id, height=rows, width=cols)


def read_exec(session: ExecTty) -> bytes | None:
    if session.closed:
        return None
    try:
        with session.lock:
            if session.closed:
                return None
            data = _sock_recv(session.sock, 8192)
    except (TimeoutError, socket.timeout):
        return b""
    except Exception as exc:
        if "timeout" in type(exc).__name__.lower():
            return b""
        return None
    if not data:
        return None
    return bytes(data)


def write_exec(session: ExecTty, data: bytes) -> None:
    if session.closed or not data:
        return
    with session.lock:
        if session.closed:
            return
        _sock_settimeout(session.sock, None)
        try:
            _sock_send(session.sock, data)
        finally:
            _sock_settimeout(session.sock, 0.5)
