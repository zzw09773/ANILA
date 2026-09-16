import React, { useState } from "react";

/**
 * 邊界訊息上方的分隔線：舊內容仍可捲動閱讀，但模型只看得到摘要。
 */
export function CompactBoundaryBanner({ summary, onRestore }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      data-compact-boundary=""
      data-testid="compact-boundary"
      style={{
        margin: "12px 0 16px",
        paddingTop: 12,
        borderTop: "1px dashed var(--border)",
        fontSize: 12,
        lineHeight: 1.65,
        color: "var(--fg-muted)",
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
        <span>以上內容已摘要，模型只看得到摘要</span>
        <span aria-hidden="true">·</span>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          style={{
            background: "none",
            border: "none",
            padding: 0,
            color: "var(--accent)",
            cursor: "pointer",
            fontSize: 12,
          }}
        >
          查看摘要
        </button>
        <span aria-hidden="true">·</span>
        <button
          type="button"
          onClick={onRestore}
          style={{
            background: "none",
            border: "none",
            padding: 0,
            color: "var(--accent)",
            cursor: "pointer",
            fontSize: 12,
          }}
        >
          還原完整上下文
        </button>
      </div>
      {open ? (
        <pre
          data-testid="compact-summary-text"
          style={{
            margin: "10px 0 0",
            padding: "10px 12px",
            background: "var(--bg-subtle)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            fontSize: 12,
            lineHeight: 1.6,
            color: "var(--fg)",
            whiteSpace: "pre-wrap",
            fontFamily: "var(--font-mono)",
          }}
        >
          {summary}
        </pre>
      ) : null}
    </div>
  );
}
