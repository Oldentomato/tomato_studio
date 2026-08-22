export type WorkspaceStatus = "stopped" | "starting" | "running" | "stopping" | "error";

export type Workspace = {
  id: string;
  name: string;
  slug: string;
  status: WorkspaceStatus;
  url: string | null;
  host_port: number | null;
  error_message: string | null;
  spec_id: string | null;
  memory_limit: string | null;
  pip_packages: string[];
  apt_packages: string[];
  docker_image: string | null;
  kind?: "vscode" | "container" | "web";
  http_port?: number | null;
  hostname?: string | null;
  access?: {
    network?: string;
    hostname?: string;
    aliases?: string[];
    port?: number;
    user?: string;
    password?: string;
    database?: string;
    ui_path?: string;
  } | null;
  logs: string[];
  created_at: string;
  last_accessed_at: string;
};

export type Spec = {
  id: string;
  name: string;
  summary: string;
  docker_image: string;
  memory: string;
  python_version: string;
  pip_packages: string[];
  apt_packages: string[];
  kind?: "vscode" | "container" | "web";
  http_port?: number | null;
  access?: {
    network?: string;
    hostname?: string;
    aliases?: string[];
    port?: number;
    user?: string;
    password?: string;
    database?: string;
    ui_path?: string;
  } | null;
  notes: string;
  markdown?: string;
  workspace_id: string | null;
  created_at: string;
};

export type ToolTrace = {
  name: string;
  ok: boolean;
  summary: string;
};

export type ChatOut = {
  conversation_id: string;
  reply: string;
  spec: Spec | null;
  workspace: Workspace | null;
  tools: ToolTrace[];
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  tools?: ToolTrace[];
};

export type FileEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  mtime: number | null;
};

export type FileList = {
  path: string;
  entries: FileEntry[];
};

export type ChatStreamEvent =
  | { type: "conversation"; conversation_id: string }
  | { type: "status"; message: string }
  | { type: "tool_start"; name: string }
  | { type: "tool_result"; tool: ToolTrace }
  | { type: "spec"; spec: Spec }
  | { type: "workspace"; workspace: Workspace }
  | { type: "done"; result: ChatOut }
  | { type: "error"; message: string };

async function parseError(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: string };
    if (typeof data.detail === "string") return data.detail;
  } catch {
    /* ignore */
  }
  return `요청이 실패했습니다 (${response.status})`;
}

export async function listWorkspaces(): Promise<Workspace[]> {
  const response = await fetch("/api/workspaces");
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function createWorkspace(name: string): Promise<Workspace> {
  const response = await fetch("/api/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getWorkspace(id: string): Promise<Workspace> {
  const response = await fetch(`/api/workspaces/${id}`);
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function startWorkspace(id: string): Promise<Workspace> {
  const response = await fetch(`/api/workspaces/${id}/start`, { method: "POST" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function stopWorkspace(id: string): Promise<Workspace> {
  const response = await fetch(`/api/workspaces/${id}/stop`, { method: "POST" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function heartbeatWorkspace(id: string): Promise<Workspace> {
  const response = await fetch(`/api/workspaces/${id}/heartbeat`, { method: "POST" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteWorkspace(id: string): Promise<void> {
  const response = await fetch(`/api/workspaces/${id}`, { method: "DELETE" });
  if (!response.ok) throw new Error(await parseError(response));
}

export function workspaceTerminalUrl(id: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/workspaces/${id}/terminal`;
}

export function workspaceFileUrl(id: string, path: string): string {
  return `/api/workspaces/${id}/files/content?path=${encodeURIComponent(path)}`;
}

export async function listWorkspaceFiles(id: string, path = ""): Promise<FileList> {
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  const response = await fetch(`/api/workspaces/${id}/files${qs}`);
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function uploadWorkspaceFiles(id: string, path: string, files: File[]): Promise<FileList> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const qs = path ? `?path=${encodeURIComponent(path)}` : "";
  const response = await fetch(`/api/workspaces/${id}/files${qs}`, { method: "POST", body: form });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function listSpecs(): Promise<Spec[]> {
  const response = await fetch("/api/specs");
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function getSpec(specId: string): Promise<Spec> {
  const response = await fetch(`/api/specs/${specId}`);
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function createFromSpec(specId: string): Promise<Workspace> {
  const response = await fetch(`/api/specs/${specId}/create`, { method: "POST" });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function sendChat(
  message: string,
  conversationId: string | null,
  workspaceId?: string | null,
): Promise<ChatOut> {
  const response = await fetch("/api/agent/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId, workspace_id: workspaceId || null }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function sendChatStream(
  message: string,
  conversationId: string | null,
  onEvent: (event: ChatStreamEvent) => void,
  workspaceId?: string | null,
): Promise<ChatOut> {
  const response = await fetch("/api/agent/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId, workspace_id: workspaceId || null }),
  });
  if (!response.ok) throw new Error(await parseError(response));
  if (!response.body) throw new Error("스트림 응답을 받을 수 없습니다.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: ChatOut | null = null;

  function handleFrame(frame: string) {
    const lines = frame.split("\n");
    let eventName = "message";
    const dataParts: string[] = [];
    for (const line of lines) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      if (line.startsWith("data:")) dataParts.push(line.slice(5).trim());
    }
    if (dataParts.length === 0) return;
    const parsed = JSON.parse(dataParts.join("\n")) as Record<string, unknown>;

    if (eventName === "conversation") onEvent({ type: "conversation", conversation_id: String(parsed.conversation_id ?? "") });
    else if (eventName === "status") onEvent({ type: "status", message: String(parsed.message ?? "") });
    else if (eventName === "tool_start") onEvent({ type: "tool_start", name: String(parsed.name ?? "") });
    else if (eventName === "tool_result") onEvent({ type: "tool_result", tool: parsed as unknown as ToolTrace });
    else if (eventName === "spec") onEvent({ type: "spec", spec: parsed as unknown as Spec });
    else if (eventName === "workspace") onEvent({ type: "workspace", workspace: parsed as unknown as Workspace });
    else if (eventName === "error") onEvent({ type: "error", message: String(parsed.message ?? "알 수 없는 오류") });
    else if (eventName === "done") {
      finalResult = parsed as unknown as ChatOut;
      onEvent({ type: "done", result: finalResult });
    }
  }

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep = buffer.indexOf("\n\n");
    while (sep >= 0) {
      const frame = buffer.slice(0, sep).trim();
      buffer = buffer.slice(sep + 2);
      if (frame) handleFrame(frame);
      sep = buffer.indexOf("\n\n");
    }
  }

  if (!finalResult) throw new Error("스트림이 비정상 종료되었습니다.");
  return finalResult;
}
