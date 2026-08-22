from __future__ import annotations

import io
import posixpath
import tarfile
import time
import uuid
from pathlib import Path

from docker.errors import APIError, DockerException, ImageNotFound, NotFound


def _client():
    from .docker_ws import docker_client

    return docker_client()

DOWNLOAD_DIR = Path("data/downloads")
PROJECT_PREFIXES = (
    "/home/coder/project/",
    "home/coder/project/",
    "/data/",
    "data/",
)


def safe_relpath(path: str) -> str:
    raw = (path or "").replace("\\", "/").strip()
    if not raw or raw in {".", "/"}:
        return ""
    for prefix in PROJECT_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {".", "/"}:
        return ""
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise ValueError("프로젝트 폴더 밖의 경로는 요청할 수 없습니다.")
    return normalized


def _helper(volume_name: str):
    client = _client()
    try:
        client.images.get("alpine:3.20")
    except (NotFound, ImageNotFound):
        client.images.pull("alpine:3.20")
    return client.containers.create(
        "alpine:3.20",
        command=["sleep", "3600"],
        volumes={volume_name: {"bind": "/data", "mode": "rw"}},
    )


def _start_helper(helper):
    helper.start()
    deadline = time.time() + 45
    while time.time() < deadline:
        helper.reload()
        if helper.status == "running":
            return helper
        time.sleep(0.4)
    raise RuntimeError("볼륨 작업용 컨테이너가 실행되지 않았습니다.")


VSCODE_DARK_SETTINGS = """{
  "workbench.colorTheme": "Default Dark Modern",
  "workbench.preferredDarkColorTheme": "Default Dark Modern",
  "window.autoDetectColorScheme": false
}
"""


def ensure_writable(volume_name: str, *, vscode_defaults: bool = False) -> None:
    helper = _helper(volume_name)
    _start_helper(helper)
    try:
        # code-server 기본 사용자(coder: 1000)가 항상 쓸 수 있게 보정
        script = "chown -R 1000:1000 /data && chmod -R u+rwX,g+rwX /data"
        if vscode_defaults:
            script += f"""
mkdir -p /data/.vscode
if [ ! -f /data/.vscode/settings.json ]; then
cat > /data/.vscode/settings.json << 'EOF'
{VSCODE_DARK_SETTINGS.strip()}
EOF
chown -R 1000:1000 /data/.vscode
fi
"""
        result = helper.exec_run(["sh", "-lc", script])
        if result.exit_code != 0:
            output = (result.output or b"").decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"볼륨 권한 보정에 실패했습니다: {output}")
    finally:
        try:
            helper.remove(force=True)
        except DockerException:
            pass


def put_text(volume_name: str, relpath: str, content: str) -> None:
    relpath = safe_relpath(relpath)
    if not relpath:
        raise ValueError("파일 경로가 필요합니다.")
    helper = _helper(volume_name)
    _start_helper(helper)
    try:
        parent = posixpath.dirname(relpath)
        if parent:
            helper.exec_run(["mkdir", "-p", f"/data/{parent}"])
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
        helper.put_archive("/data", buffer.getvalue())
    finally:
        try:
            helper.remove(force=True)
        except DockerException:
            pass
    ensure_writable(volume_name)


def get_bytes(volume_name: str, relpath: str) -> tuple[str, bytes]:
    relpath = safe_relpath(relpath)
    if not relpath:
        raise ValueError("파일 경로가 필요합니다.")
    helper = _helper(volume_name)
    _start_helper(helper)
    try:
        stream, stat = helper.get_archive(f"/data/{relpath}")
        data = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            member = next((item for item in tar.getmembers() if item.isfile()), None)
            if member is None:
                raise FileNotFoundError(relpath)
            extracted = tar.extractfile(member)
            if extracted is None:
                raise FileNotFoundError(relpath)
            filename = posixpath.basename(relpath) or member.name
            return filename, extracted.read()
    except (NotFound, APIError) as exc:
        raise FileNotFoundError(relpath) from exc
    finally:
        try:
            helper.remove(force=True)
        except DockerException:
            pass


def list_paths(volume_name: str, relpath: str = "") -> list[str]:
    target = safe_relpath(relpath)
    helper = _helper(volume_name)
    _start_helper(helper)
    try:
        location = f"/data/{target}" if target else "/data"
        result = helper.exec_run(
            [
                "sh",
                "-c",
                f"find {location} -maxdepth 3 \\( -type f -o -type d \\) | sed 's|^/data/||;s|^/data$||'",
            ]
        )
        if result.exit_code != 0:
            raise FileNotFoundError(relpath or ".")
        rows: list[str] = []
        output = (result.output or b"").decode("utf-8", errors="replace")
        for line in output.splitlines():
            name = line.strip()
            if name:
                rows.append(name)
        return rows[:200]
    finally:
        try:
            helper.remove(force=True)
        except DockerException:
            pass


def save_download(filename: str, payload: bytes) -> str:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    path = DOWNLOAD_DIR / token
    path.write_bytes(payload)
    meta = DOWNLOAD_DIR / f"{token}.name"
    meta.write_text(filename, encoding="utf-8")
    return token


def read_download(token: str) -> tuple[str, Path]:
    if not token.isalnum():
        raise FileNotFoundError(token)
    path = DOWNLOAD_DIR / token
    meta = DOWNLOAD_DIR / f"{token}.name"
    if not path.exists() or not meta.exists():
        raise FileNotFoundError(token)
    return meta.read_text(encoding="utf-8"), path
