function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function inline(value: string) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

export default function SpecMarkdown({ source }: { source: string }) {
  const blocks: string[] = [];
  const lines = (source || "").replace(/\r\n/g, "\n").split("\n");
  let list: string[] = [];

  function flushList() {
    if (list.length === 0) return;
    blocks.push(`<ul>${list.join("")}</ul>`);
    list = [];
  }

  for (const raw of lines) {
    const line = raw.trimEnd();
    const trimmed = line.trim();
    const item = trimmed.match(/^[-*]\s+(.+)$/);
    if (item) {
      list.push(`<li>${inline(item[1])}</li>`);
      continue;
    }
    flushList();
    if (!trimmed) continue;
    if (trimmed.startsWith("### ")) {
      blocks.push(`<h4>${inline(trimmed.slice(4))}</h4>`);
    } else if (trimmed.startsWith("## ")) {
      blocks.push(`<h3>${inline(trimmed.slice(3))}</h3>`);
    } else if (trimmed.startsWith("# ")) {
      blocks.push(`<h2>${inline(trimmed.slice(2))}</h2>`);
    } else {
      blocks.push(`<p>${inline(trimmed)}</p>`);
    }
  }
  flushList();

  if (blocks.length === 0) {
    return <p className="muted">사양서 본문이 없습니다.</p>;
  }

  return <div className="spec-markdown" dangerouslySetInnerHTML={{ __html: blocks.join("") }} />;
}
