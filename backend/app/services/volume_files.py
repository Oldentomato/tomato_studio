from __future__ import annotations

import io
import json
import posixpath
import tarfile
import time

from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from ..models import Workspace

HELPER_IMAGE = "alpine:3.20"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
PROJECT_PREFIXES = (
    "/home/coder/project/",
    "home/coder/project/",
    "/data/",
    "data/",
)


class ContainerNotRunning(RuntimeError):
    def __init__(self) -> None:
        super().__init__("컨테이너가 실행 중일 때만 파일을 볼 수 있습니다.")


def _client():
    from .docker_ws import docker_client

    return docker_client()


def helper_name(volume_name: str) -> str:
    return f"{volume_name}-fs"


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


def safe_filename(name: str | None) -> str:
    raw = posixpath.basename((name or "").replace("\\", "/")).strip()
    if not raw or raw in {".", ".."}:
        raise ValueError("파일 이름이 올바르지 않습니다.")
    if any(ch in raw for ch in "/\\\x00"):
        raise ValueError(f"허용되지 않는 파일 이름입니다: {name}")
    return raw[:180]


def _abs(root: str, relpath: str) -> str:
    if not relpath:
        return root
    return f"{root.rstrip('/')}/{relpath}"


def _volume_root(workspace: Workspace) -> str:
    from .docker_ws import PROJECT_DIR
    from .specs import service_volume_path, web_volume_path

    kind = (workspace.kind or "vscode").strip().lower()
    image = workspace.docker_image or ""
    if kind == "vscode":
        return PROJECT_DIR
    if kind == "web":
        return web_volume_path(image)
    return service_volume_path(image)


def _open_fs(workspace: Workspace | None):
    if workspace is None:
        raise ContainerNotRunning()
    from .docker_ws import docker_client, sync_from_docker

    sync_from_docker(workspace)
    client = docker_client()
    names = []
    if workspace.container_id:
        names.append(workspace.container_id)
    names.append(f"tomato-ws-{workspace.id}")
    for name in names:
        try:
            container = client.containers.get(name)
            container.reload()
        except (NotFound, DockerException):
            continue
        if container.status == "running":
            return container, _volume_root(workspace)
    raise ContainerNotRunning()


def remove_helper(volume_name: str) -> None:
    try:
        _client().containers.get(helper_name(volume_name)).remove(force=True)
    except (NotFound, DockerException):
        pass


VSCODE_DARK_SETTINGS = """{
  "workbench.colorTheme": "Default Dark Modern",
  "workbench.preferredDarkColorTheme": "Default Dark Modern",
  "window.autoDetectColorScheme": false
}
"""


def ensure_writable(volume_name: str, *, vscode_defaults: bool = False) -> None:
    client = _client()
    try:
        client.images.get(HELPER_IMAGE)
    except (NotFound, ImageNotFound):
        client.images.pull(HELPER_IMAGE)
    helper = client.containers.create(
        HELPER_IMAGE,
        command=["sleep", "60"],
        volumes={volume_name: {"bind": "/data", "mode": "rw"}},
    )
    helper.start()
    deadline = time.time() + 45
    while time.time() < deadline:
        helper.reload()
        if helper.status == "running":
            break
        time.sleep(0.2)
    else:
        helper.remove(force=True)
        raise RuntimeError("볼륨 작업용 컨테이너가 실행되지 않았습니다.")
    try:
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


def put_text(relpath: str, content: str, workspace: Workspace | None = None) -> None:
    put_files([(relpath, content.encode("utf-8"))], workspace=workspace)


def put_bytes(relpath: str, payload: bytes, workspace: Workspace | None = None) -> None:
    put_files([(relpath, payload)], workspace=workspace)


def put_files(
    items: list[tuple[str, bytes]],
    workspace: Workspace | None = None,
) -> None:
    cleaned: list[tuple[str, bytes]] = []
    for relpath, payload in items:
        rel = safe_relpath(relpath)
        if not rel:
            raise ValueError("파일 경로가 필요합니다.")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise ValueError("파일 크기는 64MB 이하여야 합니다.")
        cleaned.append((rel, payload))
    if not cleaned:
        return
    container, root = _open_fs(workspace)
    parents = {posixpath.dirname(rel) for rel, _ in cleaned if posixpath.dirname(rel)}
    for parent in sorted(parents):
        container.exec_run(["mkdir", "-p", _abs(root, parent)])
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for rel, payload in cleaned:
            info = tarfile.TarInfo(name=rel)
            info.size = len(payload)
            info.uid = 1000
            info.gid = 1000
            info.mode = 0o664
            tar.addfile(info, io.BytesIO(payload))
    buffer.seek(0)
    container.put_archive(root, buffer.getvalue())
    for rel, _ in cleaned:
        container.exec_run(["chown", "1000:1000", _abs(root, rel)])


def get_bytes(relpath: str, workspace: Workspace | None = None) -> tuple[str, bytes]:
    relpath = safe_relpath(relpath)
    if not relpath:
        raise ValueError("파일 경로가 필요합니다.")
    container, root = _open_fs(workspace)
    try:
        stream, _stat = container.get_archive(_abs(root, relpath))
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


def _list_script(location: str) -> str:
    loc = json.dumps(location)
    return f"""
loc={loc}
if [ ! -e "$loc" ]; then
  echo __ENOENT__
  exit 2
fi
if [ ! -d "$loc" ]; then
  echo __NOTDIR__
  exit 3
fi
ls -1A "$loc" 2>/dev/null | while IFS= read -r name; do
  [ -z "$name" ] && continue
  f="$loc/$name"
  if [ -d "$f" ]; then
    kind=d
    size=0
  else
    kind=f
    size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  fi
  mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
  printf '%s\\t%s\\t%s\\t%s\\n' "$kind" "$size" "$mtime" "$name"
done
"""


def list_entries(relpath: str = "", workspace: Workspace | None = None) -> list[dict]:
    target = safe_relpath(relpath)
    container, root = _open_fs(workspace)
    result = container.exec_run(["sh", "-c", _list_script(_abs(root, target))])
    output = (result.output or b"").decode("utf-8", errors="replace")
    if result.exit_code == 2 or "__ENOENT__" in output:
        raise FileNotFoundError(relpath or ".")
    if result.exit_code == 3 or "__NOTDIR__" in output:
        raise NotADirectoryError(relpath or ".")
    if result.exit_code != 0:
        raise RuntimeError(output[-1000:] or "디렉터리 목록을 읽지 못했습니다.")
    rows: list[dict] = []
    for line in output.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        kind, size_raw, mtime_raw, name = parts
        name = name.strip()
        if not name:
            continue
        try:
            size = int(size_raw)
        except ValueError:
            size = 0
        try:
            mtime = int(float(mtime_raw))
        except ValueError:
            mtime = 0
        is_dir = kind.strip() == "d"
        child = f"{target}/{name}" if target else name
        rows.append(
            {
                "name": name,
                "path": child,
                "is_dir": is_dir,
                "size": None if is_dir else size,
                "mtime": mtime or None,
            }
        )
    rows.sort(key=lambda item: (not item["is_dir"], str(item["name"]).lower()))
    return rows[:500]
