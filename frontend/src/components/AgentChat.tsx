import { FormEvent, useEffect, useRef, useState } from "react";
import { sendChatStream, type ChatMessage, type ChatOut, type ToolTrace, type Workspace } from "../api";

const STORAGE_KEY = "tomato.conversation_id";

type Props = {
  onResult: (result: ChatOut) => void;
  onProgress: (result: Partial<ChatOut>) => void;
  onReset?: () => void;
  selectedWorkspace?: Workspace | null;
};

export default function AgentChat({
  onResult,
  onProgress,
  onReset,
  selectedWorkspace = null,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [liveTools, setLiveTools] = useState<ToolTrace[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(() => localStorage.getItem(STORAGE_KEY));
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || pending) return;
    setInput("");
    setError(null);
    setLiveTools([]);
    setMessages((current) => [...current, { role: "user", content: text }]);
    setPending(true);
    try {
      const result = await sendChatStream(
        text,
        conversationId,
        (eventData) => {
          if (eventData.type === "conversation" && eventData.conversation_id) {
            localStorage.setItem(STORAGE_KEY, eventData.conversation_id);
            setConversationId(eventData.conversation_id);
          }
          if (eventData.type === "tool_start") {
            setLiveTools((current) => [...current, { name: eventData.name, ok: true, summary: "실행 중…" }]);
          }
          if (eventData.type === "tool_result") {
            setLiveTools((current) => {
              const next = [...current];
              const idx = next.findIndex((item) => item.name === eventData.tool.name && item.summary === "실행 중…");
              if (idx >= 0) next[idx] = eventData.tool;
              else next.push(eventData.tool);
              return next;
            });
          }
          if (eventData.type === "spec") onProgress({ spec: eventData.spec });
          if (eventData.type === "workspace") onProgress({ workspace: eventData.workspace });
        },
        selectedWorkspace?.id,
      );
      localStorage.setItem(STORAGE_KEY, result.conversation_id);
      setConversationId(result.conversation_id);
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: result.reply,
          tools: result.tools,
        },
      ]);
      onResult(result);
      setLiveTools([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "에이전트 요청이 실패했습니다.");
    } finally {
      setPending(false);
    }
  }

  function resetSession() {
    if (pending) return;
    localStorage.removeItem(STORAGE_KEY);
    setConversationId(null);
    setMessages([]);
    setInput("");
    setError(null);
    setLiveTools([]);
    onReset?.();
  }

  return (
    <section className="chat-panel">
      <div className="chat-head">
        <div>
          <h2>에이전트</h2>
          {selectedWorkspace ? (
            <p>
              선택: {selectedWorkspace.name}
              {selectedWorkspace.status === "error"
                ? " · 오류 카드를 고쳤어요. 수정해 달라고 하면 사양서를 고치고 다시 적용합니다."
                : " · 이 워크스페이스를 기준으로 답합니다."}
            </p>
          ) : (
            <p>사양서를 먼저 보여 줍니다. 컨테이너는 오른쪽 버튼으로만 만듭니다.</p>
          )}
        </div>
        <button type="button" className="ghost compact" onClick={resetSession} disabled={pending}>
          초기화
        </button>
      </div>
      <div className="chat-log" ref={scroller}>
        {messages.length === 0 ? (
          <p className="muted">예: “pandas 데이터 분석 환경 사양서 작성해줘”</p>
        ) : (
          messages.map((item, index) => (
            <article key={index} className={`bubble ${item.role}`}>
              <p>{item.content}</p>
              {item.tools && item.tools.length > 0 ? (
                <ul className="tool-list">
                  {item.tools.map((tool, toolIndex) => (
                    <li key={toolIndex} className={tool.ok ? "ok" : "fail"}>
                      {tool.name}: {tool.summary}
                    </li>
                  ))}
                </ul>
              ) : null}
            </article>
          ))
        )}
        {pending ? (
          <div>
            <p className="muted">도구를 실행하는 중…</p>
            {liveTools.length > 0 ? (
              <ul className="tool-list live">
                {liveTools.map((tool, index) => (
                  <li key={`${tool.name}-${index}`} className={tool.ok ? "ok" : "fail"}>
                    {tool.name}: {tool.summary}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
      {error ? <p className="banner error">{error}</p> : null}
      <form className="chat-form" onSubmit={onSubmit}>
        <textarea
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder={
            selectedWorkspace?.status === "error"
              ? "예: 오류 나니까 수정해서 업데이트해줘"
              : "필요한 환경을 말해 주세요"
          }
          rows={3}
          disabled={pending}
        />
        <button type="submit" disabled={pending || !input.trim()}>
          보내기
        </button>
      </form>
    </section>
  );
}
