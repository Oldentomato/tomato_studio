import { FormEvent, MouseEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import AgentChat from "../components/AgentChat";
import SpecPanel from "../components/SpecPanel";
import {
  createWorkspace,
  deleteWorkspace,
  getSpec,
  listWorkspaces,
  stopWorkspace,
  type ChatOut,
  type Spec,
  type Workspace,
} from "../api";

const STATUS_LABEL: Record<Workspace["status"], string> = {
  running: "실행 중",
  starting: "시작 중",
  stopping: "중지 중",
  stopped: "대기",
  error: "오류",
};

export default function Studio() {
  const navigate = useNavigate();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [spec, setSpec] = useState<Spec | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refreshAll() {
    const items = await listWorkspaces();
    setWorkspaces(items);
  }

  async function refresh() {
    const items = await listWorkspaces();
    setWorkspaces(items);
  }

  useEffect(() => {
    let cancelled = false;
    listWorkspaces()
      .then((items) => {
        if (cancelled) return;
        setWorkspaces(items);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const preparing = workspaces.some(
      (item) => item.status === "starting" || item.status === "stopping",
    );
    const timer = window.setInterval(() => {
      refresh().catch(() => undefined);
    }, preparing ? 2000 : 5000);
    return () => window.clearInterval(timer);
  }, [workspaces]);

  function onAgentResult(result: ChatOut) {
    if (result.spec) setSpec(result.spec);
    if (result.workspace) {
      setWorkspaces((current) => {
        const rest = current.filter((item) => item.id !== result.workspace?.id);
        return [result.workspace as Workspace, ...rest];
      });
    }
    void refresh();
  }

  async function onCreate(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    setError(null);
    try {
      const created = await createWorkspace(trimmed);
      setName("");
      setWorkspaces((current) => [created, ...current]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "만들지 못했습니다.");
    }
  }

  async function onLaunch(event: MouseEvent, workspace: Workspace) {
    event.stopPropagation();
    const kind = workspace.kind ?? "vscode";
    navigate(kind === "container" ? `/term/${workspace.id}` : `/ws/${workspace.id}`);
  }

  async function onStop(event: MouseEvent, id: string) {
    event.stopPropagation();
    setBusyId(id);
    setError(null);
    try {
      const updated = await stopWorkspace(id);
      setWorkspaces((current) => current.map((item) => (item.id === id ? updated : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : "중지하지 못했습니다.");
    } finally {
      setBusyId(null);
    }
  }

  async function onCardClick(workspace: Workspace) {
    setSelectedId(workspace.id);
    if (workspace.spec_id) {
      try {
        const loaded = await getSpec(workspace.spec_id);
        setSpec(loaded);
      } catch {
        // spec이 없으면 패널은 비워둠
        setSpec(null);
      }
    } else {
      setSpec(null);
    }
  }

  async function onDelete(event: MouseEvent, workspace: Workspace) {
    event.stopPropagation();
    const ok = window.confirm(`‘${workspace.name}’ 워크스페이스와 파일을 삭제할까요?`);
    if (!ok) return;
    setBusyId(workspace.id);
    setError(null);
    try {
      await deleteWorkspace(workspace.id);
      setWorkspaces((current) => current.filter((item) => item.id !== workspace.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "삭제하지 못했습니다.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="page studio-page">
      <header className="hero compact">
        <div className="brand">
          <span className="tomato" aria-hidden="true" />
          <div>
            <p className="eyebrow">personal coding studio</p>
            <h1>Tomato Studio</h1>
          </div>
        </div>
      </header>

      <div className="studio-layout">
        <AgentChat
          selectedWorkspace={workspaces.find((item) => item.id === selectedId) ?? null}
          onResult={onAgentResult}
          onProgress={(partial) => {
            if (partial.spec) setSpec(partial.spec);
            if (partial.workspace) {
              setWorkspaces((current) => {
                const rest = current.filter((item) => item.id !== partial.workspace?.id);
                return [partial.workspace as Workspace, ...rest];
              });
            }
          }}
        />
        <div className="studio-side">
          <SpecPanel
            spec={spec}
            onCreated={(workspace) => {
              setSpec((current) =>
                current
                  ? {
                      ...current,
                      workspace_id: workspace.id,
                      access: workspace.access ?? current.access,
                      markdown: (current.markdown || "").replaceAll(
                        "tomato-ws-<id>",
                        `tomato-ws-${workspace.id}`,
                      ),
                    }
                  : current,
              );
              void refreshAll();
            }}
          />
          {error ? <p className="banner error">{error}</p> : null}
          <section className="workspace-section">
            <div className="section-head">
              <h2>워크스페이스</h2>
              <form className="create compact-form" onSubmit={onCreate}>
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="직접 만들기"
                  maxLength={80}
                />
                <button type="submit" disabled={!name.trim()}>
                  추가
                </button>
              </form>
            </div>
            {loading ? (
              <p className="muted">목록을 불러오는 중…</p>
            ) : workspaces.length === 0 ? (
              <div className="empty">에이전트에게 환경을 요청하거나, 직접 추가하세요.</div>
            ) : (
              <ul className="grid">
                {workspaces.map((workspace) => {
                  const kind = workspace.kind ?? "vscode";
                  const isVscode = kind === "vscode";
                  const isWeb = kind === "web";
                  const preparing = workspace.status === "starting" || workspace.status === "stopping";
                  const canLaunch =
                    (workspace.status === "running" || workspace.status === "stopped" || workspace.status === "error") &&
                    busyId !== workspace.id;
                  const actionsLocked = preparing || busyId === workspace.id;
                  const kindLabel = isVscode ? "VS Code" : isWeb ? "웹 UI" : "컨테이너";
                  const launchLabel =
                    workspace.status === "starting"
                      ? "준비 중…"
                      : workspace.status === "stopping"
                        ? "중지 중…"
                        : workspace.status === "running"
                          ? kind === "container"
                            ? "터미널"
                            : "열기"
                          : "시작";
                  return (
                  <li key={workspace.id}>
                    <button
                      type="button"
                      className={`card${selectedId === workspace.id ? " selected" : ""}${preparing ? " preparing" : ""}`}
                      onClick={() => onCardClick(workspace)}
                    >
                      <div className="card-top">
                        <span className={`status ${workspace.status}`}>
                          <i />
                          {STATUS_LABEL[workspace.status]}
                        </span>
                        <span className="slug">
                          {kindLabel} · {workspace.memory_limit ?? "2g"}
                        </span>
                      </div>
                      <h2>{workspace.name}</h2>
                      <p className="hint">
                        {workspace.docker_image ?? "code-server"}
                        {!isVscode && workspace.hostname ? ` · ${workspace.hostname}` : ""}
                        {isWeb && workspace.http_port ? ` · :${workspace.http_port}` : ""}
                        {workspace.pip_packages?.length ? ` · ${workspace.pip_packages.slice(0, 3).join(", ")}` : ""}
                        {workspace.apt_packages?.length ? ` · apt ${workspace.apt_packages.slice(0, 2).join(", ")}` : ""}
                      </p>
                      {workspace.status === "error" && workspace.error_message ? (
                        <p className="hint error-text">{workspace.error_message}</p>
                      ) : null}
                      {workspace.logs?.length ? (
                        <pre className="build-log" onClick={(event) => event.stopPropagation()}>
                          {workspace.logs.slice(-12).join("\n")}
                        </pre>
                      ) : null}
                      <div className="actions">
                        <button
                          type="button"
                          className="ghost"
                          disabled={!canLaunch}
                          onClick={(event) => {
                            if (!canLaunch) {
                              event.stopPropagation();
                              return;
                            }
                            void onLaunch(event, workspace);
                          }}
                        >
                          {launchLabel}
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={busyId === workspace.id}
                          onClick={(event) => {
                            event.stopPropagation();
                            navigate(`/files/${workspace.id}`);
                          }}
                        >
                          파일
                        </button>
                        <button
                          type="button"
                          className="ghost"
                          disabled={workspace.status !== "running" || actionsLocked}
                          onClick={(event) => onStop(event, workspace.id)}
                        >
                          중지
                        </button>
                        <button
                          type="button"
                          className="ghost danger"
                          disabled={actionsLocked}
                          onClick={(event) => onDelete(event, workspace)}
                        >
                          삭제
                        </button>
                      </div>
                    </button>
                  </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
