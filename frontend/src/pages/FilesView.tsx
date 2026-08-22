import { ChangeEvent, DragEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  getWorkspace,
  heartbeatWorkspace,
  listWorkspaceFiles,
  startWorkspace,
  uploadWorkspaceFiles,
  workspaceFileUrl,
  type FileEntry,
  type Workspace,
} from "../api";

function formatSize(entry: FileEntry): string {
  if (entry.is_dir) return "폴더";
  const n = entry.size ?? 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function formatTime(mtime: number | null): string {
  if (!mtime) return "—";
  return new Date(mtime * 1000).toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function FilesView() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [starting, setStarting] = useState(true);
  const [dragging, setDragging] = useState(false);

  const crumbs = useMemo(() => {
    const parts = path.split("/").filter(Boolean);
    const rows: { label: string; path: string }[] = [{ label: "루트", path: "" }];
    let current = "";
    for (const part of parts) {
      current = current ? `${current}/${part}` : part;
      rows.push({ label: part, path: current });
    }
    return rows;
  }, [path]);

  const load = useCallback(
    async (nextPath: string) => {
      if (!id) return;
      setLoading(true);
      setError(null);
      try {
        const listed = await listWorkspaceFiles(id, nextPath);
        setPath(listed.path);
        setEntries(listed.entries);
      } catch (err) {
        setError(err instanceof Error ? err.message : "목록을 불러오지 못했습니다.");
      } finally {
        setLoading(false);
      }
    },
    [id],
  );

  useEffect(() => {
    if (!id) return;
    const workspaceId = id;
    let cancelled = false;

    async function boot() {
      setStarting(true);
      setError(null);
      try {
        let current = await getWorkspace(workspaceId);
        if (cancelled) return;
        setWorkspace(current);
        if (current.status !== "running") {
          current = await startWorkspace(workspaceId);
          if (cancelled) return;
          setWorkspace(current);
        }
        const listed = await listWorkspaceFiles(workspaceId, "");
        if (cancelled) return;
        setPath(listed.path);
        setEntries(listed.entries);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "파일 화면을 열지 못했습니다.");
        }
      } finally {
        if (!cancelled) {
          setStarting(false);
          setLoading(false);
        }
      }
    }

    void boot();
    return () => {
      cancelled = true;
    };
  }, [id]);

  useEffect(() => {
    if (!id || !workspace || workspace.status !== "running") return;
    const timer = window.setInterval(() => {
      heartbeatWorkspace(id).catch(() => undefined);
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [id, workspace]);

  async function onUpload(fileList: FileList | File[] | null) {
    if (!id || !fileList || uploading) return;
    const files = Array.from(fileList);
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const listed = await uploadWorkspaceFiles(id, path, files);
      setPath(listed.path);
      setEntries(listed.entries);
    } catch (err) {
      setError(err instanceof Error ? err.message : "업로드에 실패했습니다.");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    void onUpload(event.target.files);
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void onUpload(event.dataTransfer.files);
  }

  const parentPath = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";

  return (
    <div className="workspace-shell">
      <header className="workspace-bar">
        <button type="button" className="back" onClick={() => navigate("/")}>
          ← 스튜디오
        </button>
        <div className="workspace-title">
          <strong>{workspace?.name ?? "파일"}</strong>
          <span className="slug">볼륨 파일</span>
        </div>
        <button
          type="button"
          className="ghost"
          onClick={() => inputRef.current?.click()}
          disabled={uploading || starting || loading}
        >
          {uploading ? "올리는 중…" : "업로드"}
        </button>
      </header>
      <input ref={inputRef} type="file" multiple hidden onChange={onPick} />
      <div
        className={`files-wrap${dragging ? " dragging" : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node)) return;
          setDragging(false);
        }}
        onDrop={onDrop}
      >
        <nav className="files-crumbs">
          {crumbs.map((crumb, index) => (
            <span key={`${crumb.path}-${index}`}>
              {index > 0 ? <span className="sep">/</span> : null}
              <button type="button" onClick={() => void load(crumb.path)} disabled={crumb.path === path}>
                {crumb.label}
              </button>
            </span>
          ))}
        </nav>
        {error ? <p className="banner error">{error}</p> : null}
        {loading ? (
          <div className="workspace-message">
            <div className="pulse" />
            <p>{starting ? "컨테이너를 켠 뒤 디렉터리를 엽니다." : "디렉터리를 불러오는 중…"}</p>
          </div>
        ) : (
          <div className="files-table-wrap">
            <table className="files-table">
              <thead>
                <tr>
                  <th>이름</th>
                  <th>크기</th>
                  <th>수정</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {path ? (
                  <tr className="dir" onClick={() => void load(parentPath)}>
                    <td colSpan={4}>..</td>
                  </tr>
                ) : null}
                {entries.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="empty-row">
                      이 폴더가 비어 있습니다. 파일을 끌어다 놓거나 업로드하세요.
                    </td>
                  </tr>
                ) : (
                  entries.map((entry) => (
                    <tr
                      key={entry.path}
                      className={entry.is_dir ? "dir" : ""}
                      onClick={() => {
                        if (entry.is_dir) void load(entry.path);
                      }}
                    >
                      <td>
                        <span className="file-name">
                          {entry.is_dir ? "📁" : "📄"} {entry.name}
                        </span>
                      </td>
                      <td>{formatSize(entry)}</td>
                      <td>{formatTime(entry.mtime)}</td>
                      <td>
                        {entry.is_dir ? null : (
                          <a
                            className="ghost compact"
                            href={id ? workspaceFileUrl(id, entry.path) : "#"}
                            onClick={(event) => event.stopPropagation()}
                          >
                            받기
                          </a>
                        )}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
        {dragging ? <div className="files-drop">여기에 놓으면 이 폴더로 올라갑니다</div> : null}
      </div>
    </div>
  );
}
