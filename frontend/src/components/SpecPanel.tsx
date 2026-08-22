import { useState } from "react";
import { createFromSpec, type Spec, type Workspace } from "../api";
import SpecMarkdown from "./SpecMarkdown";

type Props = {
  spec: Spec | null;
  onCreated: (workspace: Workspace) => void;
};

export default function SpecPanel({ spec, onCreated }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onCreate() {
    if (!spec || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createFromSpec(spec.id);
      onCreated(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : "컨테이너를 만들지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  if (!spec) {
    return (
      <section className="spec-panel empty-spec">
        <h2>사양서</h2>
        <p className="muted">에이전트가 요청을 판단해 마크다운 사양서를 여기에 정리합니다.</p>
      </section>
    );
  }

  return (
    <section className="spec-panel">
      <div className="spec-head">
        <p className="eyebrow">environment spec</p>
        <p className="spec-meta">
          {spec.kind === "container" ? "일반 컨테이너" : "VS Code"} · {spec.docker_image} · {spec.memory}
        </p>
      </div>
      <SpecMarkdown source={spec.markdown || spec.summary} />
      {error ? <p className="banner error">{error}</p> : null}
      {spec.workspace_id ? (
        <div className="spec-actions">
          <p className="hint">
            연결된 컨테이너: <code>{spec.workspace_id}</code>
          </p>
          <button type="button" className="spec-create update" onClick={onCreate} disabled={busy}>
            {busy ? "업데이트 중…" : "컨테이너 업데이트"}
          </button>
        </div>
      ) : (
        <button type="button" className="spec-create" onClick={onCreate} disabled={busy}>
          {busy ? "컨테이너 만드는 중…" : "컨테이너 만들기"}
        </button>
      )}
    </section>
  );
}
