import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import "@xterm/xterm/css/xterm.css";
import {
  getWorkspace,
  heartbeatWorkspace,
  startWorkspace,
  stopWorkspace,
  workspaceTerminalUrl,
  type Workspace,
} from "../api";

export default function TerminalView() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(true);
  const [stopping, setStopping] = useState(false);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!id) return;
    const workspaceId = id;
    let cancelled = false;

    async function openTerminal() {
      setStarting(true);
      setError(null);
      try {
        let current = await getWorkspace(workspaceId);
        if (cancelled) return;
        if ((current.kind ?? "vscode") === "vscode") {
          navigate(`/ws/${workspaceId}`, { replace: true });
          return;
        }
        setWorkspace(current);
        if (current.status !== "running") {
          current = await startWorkspace(workspaceId);
          if (cancelled) return;
          setWorkspace(current);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "컨테이너를 열지 못했습니다.");
        }
      } finally {
        if (!cancelled) setStarting(false);
      }
    }

    void openTerminal();
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

  useEffect(() => {
    if (!id || starting || error || workspace?.status !== "running" || !hostRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.25,
      theme: {
        background: "#0f0b0a",
        foreground: "#f4ebe4",
        cursor: "#ff4d3d",
        cursorAccent: "#0f0b0a",
        selectionBackground: "#3a221c",
        black: "#1c1412",
        red: "#ff8a7a",
        green: "#8fd18c",
        yellow: "#e7c27a",
        blue: "#8bb4ff",
        magenta: "#d7a0ff",
        cyan: "#7fd4c8",
        white: "#f4ebe4",
        brightBlack: "#a89086",
        brightRed: "#ffb4ab",
        brightGreen: "#b6e3b3",
        brightYellow: "#f3d9a4",
        brightBlue: "#b7ccff",
        brightMagenta: "#e6c4ff",
        brightCyan: "#b7eee6",
        brightWhite: "#fff8f5",
      },
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(hostRef.current);
    term.focus();

    const socket = new WebSocket(workspaceTerminalUrl(id));
    socket.binaryType = "arraybuffer";
    const encoder = new TextEncoder();

    function sendResize() {
      fitAddon.fit();
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
    }

    socket.onopen = () => {
      setConnected(true);
      sendResize();
    };
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data) as { type?: string; message?: string };
          if (payload.type === "error" && payload.message) {
            term.writeln(`\r\n\x1b[31m${payload.message}\x1b[0m`);
            setError(payload.message);
          } else {
            term.write(event.data);
          }
        } catch {
          term.write(event.data);
        }
        return;
      }
      term.write(new Uint8Array(event.data as ArrayBuffer));
    };
    socket.onerror = () => {
      setError("터미널 연결에 실패했습니다.");
    };
    socket.onclose = () => {
      setConnected(false);
      term.writeln("\r\n\x1b[90m연결이 종료되었습니다.\x1b[0m");
    };

    const dataSub = term.onData((data) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(encoder.encode(data));
      }
    });

    const observer = new ResizeObserver(() => sendResize());
    observer.observe(hostRef.current);
    window.setTimeout(sendResize, 40);

    return () => {
      dataSub.dispose();
      observer.disconnect();
      socket.close();
      term.dispose();
      setConnected(false);
    };
  }, [id, starting, error, workspace?.status]);

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

  const ready = workspace?.status === "running" && !starting && !error;

  return (
    <div className="workspace-shell">
      <header className="workspace-bar">
        <button type="button" className="back" onClick={() => navigate("/")}>
          ← 스튜디오
        </button>
        <div className="workspace-title">
          <strong>{workspace?.name ?? "컨테이너"}</strong>
          <span className={`status ${workspace?.status ?? "starting"}`}>
            <i />
            {starting ? "시작 중" : connected ? "터미널 연결됨" : workspace?.status === "running" ? "실행 중" : workspace?.status ?? ""}
          </span>
        </div>
        <span className="slug">{workspace?.docker_image ?? ""}</span>
        <button type="button" className="ghost" onClick={onStop} disabled={!workspace || stopping}>
          중지하고 나가기
        </button>
      </header>
      {error && !ready ? (
        <div className="workspace-message">
          <p>{error}</p>
          <button type="button" onClick={() => navigate("/")}>
            스튜디오로
          </button>
        </div>
      ) : ready ? (
        <div className="terminal-wrap" ref={hostRef} />
      ) : (
        <div className="workspace-message">
          <div className="pulse" />
          <p>컨테이너를 준비하고 터미널에 연결합니다.</p>
        </div>
      )}
    </div>
  );
}
