import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  getWorkspace,
  heartbeatWorkspace,
  startWorkspace,
  stopWorkspace,
  type Workspace,
} from "../api";

export default function WorkspaceView() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(true);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    if (!id) return;
    const workspaceId = id;
    let cancelled = false;

    async function openWorkspace() {
      setStarting(true);
      setError(null);
      try {
        let current = await getWorkspace(workspaceId);
        if (cancelled) return;
        if ((current.kind ?? "vscode") === "container") {
          navigate(`/term/${workspaceId}`, { replace: true });
          return;
        }
        setWorkspace(current);
        if (current.status !== "running" || !current.url) {
          current = await startWorkspace(workspaceId);
          if (cancelled) return;
          setWorkspace(current);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "워크스페이스를 열지 못했습니다.");
        }
      } finally {
        if (!cancelled) setStarting(false);
      }
    }

    openWorkspace();
    return () => {
      cancelled = true;
    };
  }, [id, navigate]);

  useEffect(() => {
    if (!id || !workspace || workspace.status !== "running") return;
    const timer = window.setInterval(() => {
      heartbeatWorkspace(id).catch(() => undefined);
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [id, workspace]);

  async function onStop() {
    if (!id) return;
    setStopping(true);
    try {
      const updated = await stopWorkspace(id);
      setWorkspace(updated);
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "중지하지 못했습니다.");
      setStopping(false);
    }
  }

  const ready = workspace?.status === "running" && Boolean(workspace.url) && !starting;

  return (
    <div className="workspace-shell">
      <header className="workspace-bar">
        <button type="button" className="back" onClick={() => navigate("/")}>
          ← 스튜디오
        </button>
        <div className="workspace-title">
          <strong>{workspace?.name ?? "워크스페이스"}</strong>
          <span className={`status ${workspace?.status ?? "starting"}`}>
            <i />
            {starting ? "시작 중" : workspace?.status === "running" ? "실행 중" : workspace?.status ?? ""}
          </span>
        </div>
        <button type="button" className="ghost" onClick={() => id && navigate(`/files/${id}`)} disabled={!workspace}>
          파일
        </button>
        <button type="button" className="ghost" onClick={onStop} disabled={!workspace || stopping}>
          중지하고 나가기
        </button>
      </header>
      {error ? (
        <div className="workspace-message">
          <p>{error}</p>
          <button type="button" onClick={() => navigate("/")}>
            스튜디오로
          </button>
        </div>
      ) : ready && workspace?.url ? (
        <iframe
          className="editor"
          src={workspace.url}
          title={workspace.name}
          allow="clipboard-read; clipboard-write; fullscreen"
        />
      ) : (
        <div className="workspace-message">
          <div className="pulse" />
          <p>
            {workspace?.kind === "web"
              ? "웹 화면을 준비하고 있습니다. 처음이면 이미지 때문에 조금 걸릴 수 있습니다."
              : "VS Code 컨테이너를 준비하고 있습니다. 처음이면 이미지 때문에 조금 걸릴 수 있습니다."}
          </p>
        </div>
      )}
    </div>
  );
}
