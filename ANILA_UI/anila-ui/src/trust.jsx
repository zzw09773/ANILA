// Trust & transparency components (ESM)
import React, { useState } from "react";
import { IconBook, IconX, IconExternal, IconShield, IconGauge } from "./icons.jsx";
import { IconButton } from "./components.jsx";
import { renderWithRedaction } from "./data.jsx";

// ---- Inline citation [N] ----
export const CitationInline = ({ n, citation, onOpen }) => (
  <button
    onClick={() => onOpen?.(citation)}
    title={citation ? `${citation.title} · ${citation.section}` : ""}
    style={{
      display: "inline-flex", alignItems: "center", justifyContent: "center",
      minWidth: 18, height: 18, padding: "0 4px",
      marginLeft: 2, verticalAlign: "2px",
      fontSize: 10, fontWeight: 600,
      fontFamily: "var(--font-mono)",
      background: "var(--accent-soft)",
      color: "var(--accent)",
      border: "1px solid var(--accent)",
      borderRadius: 4,
      cursor: "pointer",
      lineHeight: 1,
    }}
  >[{n}]</button>
);

// Render assistant text, replacing [N] markers with CitationInline
export const renderTextWithCitations = (text, citations, onOpen) => {
  if (!text) return null;
  if (!citations || citations.length === 0) return text;
  const re = /\[(\d+)\]/g;
  const parts = [];
  let last = 0, m, key = 0;
  while ((m = re.exec(text)) !== null) {
    const n = parseInt(m[1], 10);
    if (m.index > last) parts.push(<React.Fragment key={"t" + key++}>{text.slice(last, m.index)}</React.Fragment>);
    const cit = citations[n - 1];
    parts.push(<CitationInline key={"c" + key++} n={n} citation={cit} onOpen={onOpen} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(<React.Fragment key={"t" + key++}>{text.slice(last)}</React.Fragment>);
  return parts;
};

// ---- Citations Drawer ----
export const CitationsDrawer = ({ open, citations, activeId, onClose, onJumpTo }) => {
  if (!open) return null;
  return (
    <div style={{
      width: 380, flexShrink: 0,
      borderLeft: "1px solid var(--border)",
      background: "var(--bg-subtle)",
      display: "flex", flexDirection: "column",
      height: "100%",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: 8,
        padding: "12px 14px",
        borderBottom: "1px solid var(--border)",
        background: "var(--bg)",
      }}>
        <IconBook size={14} />
        <div style={{ fontWeight: 600, fontSize: 13 }}>來源</div>
        <span style={{
          fontFamily: "var(--font-mono)", fontSize: 11,
          color: "var(--fg-subtle)",
          padding: "1px 7px",
          background: "var(--bg-subtle)",
          border: "1px solid var(--border)",
          borderRadius: 999,
        }}>{citations.length} 筆</span>
        <div style={{ flex: 1 }} />
        <IconButton onClick={onClose}><IconX /></IconButton>
      </div>
      <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px 14px" }}>
        {citations.map((c, i) => (
          <div key={c.id}
            data-cit-id={c.id}
            style={{
              padding: 12, marginBottom: 8,
              background: "var(--bg-elev)",
              border: "1px solid " + (activeId === c.id ? "var(--accent)" : "var(--border)"),
              borderRadius: "var(--radius)",
              boxShadow: activeId === c.id ? "0 0 0 3px oklch(0.58 0.08 200 / 0.2)" : "none",
              transition: "all .15s",
            }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
              <span style={{
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                width: 20, height: 20,
                fontSize: 10, fontWeight: 600,
                fontFamily: "var(--font-mono)",
                background: "var(--accent-soft)",
                color: "var(--accent)",
                border: "1px solid var(--accent)",
                borderRadius: 4,
              }}>{i + 1}</span>
              <div style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>{c.title}</div>
              {typeof c.score === "number" && (
                <span style={{
                  fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--fg-subtle)",
                }}>{Math.round(c.score * 100)}%</span>
              )}
            </div>
            {c.section && <div style={{ fontSize: 11, color: "var(--fg-muted)", marginBottom: 6 }}>{c.section}</div>}
            {c.snippet && (
              <div style={{
                padding: "7px 9px",
                background: "var(--bg-subtle)",
                borderLeft: "2px solid var(--border-strong)",
                fontSize: 12, lineHeight: 1.6, color: "var(--fg)",
                borderRadius: 3,
                marginBottom: 8,
              }}>{c.snippet}</div>
            )}
            {typeof c.score === "number" && (
              <div style={{
                height: 3, background: "var(--bg-subtle)",
                borderRadius: 999, overflow: "hidden", marginBottom: 8,
              }}>
                <div style={{
                  width: `${Math.round(c.score * 100)}%`, height: "100%",
                  background: "var(--accent)",
                }} />
              </div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {c.updated_at && (
                <span style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
                  updated {c.updated_at}
                </span>
              )}
              <div style={{ flex: 1 }} />
              {c.source_uri && (
                <button
                  onClick={() => onJumpTo?.(c)}
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 4,
                    padding: "3px 8px",
                    background: "transparent",
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius)",
                    fontSize: 11, color: "var(--fg-muted)",
                    cursor: "pointer",
                  }}>
                  <IconExternal size={11} />開啟原文
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

// ---- Redaction composer hint ----
export const RedactionHint = ({ hits, mode, onChangeMode }) => {
  if (!hits || hits.length === 0) return null;
  const byKind = {};
  hits.forEach(h => { byKind[h.label] = (byKind[h.label] || 0) + 1; });
  const summary = Object.entries(byKind).map(([k, v]) => `${v} ${k}`).join(" · ");

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "6px 10px",
      background: "oklch(0.92 0.08 70 / 0.4)",
      borderBottom: "1px solid oklch(0.72 0.14 75 / 0.5)",
      fontSize: 11.5,
      color: "var(--fg)",
    }}>
      <IconShield size={13} style={{ color: "var(--warn)" }} />
      <span>
        偵測到 <b>{hits.length}</b> 個敏感片段（{summary}）·
        {mode === "mask" ? " 送出時將自動遮罩" : mode === "warn" ? " 送出時會警告" : " 將阻擋送出"}
      </span>
      <div style={{ flex: 1 }} />
      <div style={{ display: "flex", gap: 2 }}>
        {["warn", "mask", "block"].map(m => (
          <button key={m} onClick={() => onChangeMode?.(m)} style={{
            padding: "2px 7px",
            fontSize: 10.5, fontFamily: "var(--font-mono)",
            background: m === mode ? "var(--bg-elev)" : "transparent",
            border: "1px solid " + (m === mode ? "var(--border-strong)" : "transparent"),
            borderRadius: 3, cursor: "pointer", color: "var(--fg-muted)",
          }}>{m}</button>
        ))}
      </div>
    </div>
  );
};

// ---- RedactedSpan (in user bubble) ----
export const RedactedSpan = ({ kind, label, masked }) => (
  <span
    title={`已於 CSP 層遮罩 · kind=${kind} · LLM 未接觸原值`}
    style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "0 6px",
      background: "oklch(0.82 0.12 70 / 0.35)",
      borderBottom: "1px dashed var(--warn)",
      borderRadius: 3,
      fontFamily: "var(--font-mono)", fontSize: 12,
      color: "var(--fg)",
    }}>
    <IconShield size={10} />
    <span>{masked}</span>
    <span style={{ fontSize: 9, color: "var(--fg-subtle)", marginLeft: 2 }}>[{label}]</span>
  </span>
);

export const RenderRedactedText = ({ text, hits }) => {
  const parts = renderWithRedaction(text, hits);
  if (typeof parts === "string") return parts;
  return parts.map((p, i) => {
    if (typeof p === "string") return <React.Fragment key={i}>{p}</React.Fragment>;
    return <RedactedSpan key={i} kind={p.kind} label={p.label} masked={p.masked} />;
  });
};

// ---- Confidence chip ----
export const ConfidenceChip = ({ confidence }) => {
  if (!confidence) return null;
  const { level, score } = confidence;
  const colors = {
    high:   { bg: "oklch(0.90 0.08 150 / 0.35)", fg: "var(--success)",  dot: "●", label: "高信心" },
    medium: { bg: "oklch(0.92 0.09 75 / 0.35)",  fg: "var(--warn)",     dot: "◐", label: "中等" },
    low:    { bg: "oklch(0.90 0.08 25 / 0.35)",  fg: "var(--danger)",   dot: "○", label: "低信心" },
  }[level] || {};
  return (
    <span
      title={`confidence ${score?.toFixed(2)} · reasons: ${(confidence.reasons || []).join(", ") || "n/a"}`}
      style={{
        display: "inline-flex", alignItems: "center", gap: 5,
        padding: "1px 7px",
        background: colors.bg,
        color: colors.fg,
        border: "1px solid transparent",
        borderRadius: 999,
        fontSize: 10.5, fontFamily: "var(--font-mono)",
        fontWeight: 500,
      }}>
      <span>{colors.dot}</span>
      {colors.label}
      <span style={{ color: "var(--fg-subtle)" }}>{score ? score.toFixed(2) : ""}</span>
    </span>
  );
};

// ---- Follow-up suggestions (shown when confidence is low/medium) ----
export const FollowUpSuggestions = ({ suggestions, confidence, onPick }) => {
  if (!suggestions || suggestions.length === 0) return null;
  const isLow = confidence?.level === "low" || confidence?.level === "medium";
  if (!isLow) return null;

  return (
    <div style={{
      marginTop: 10,
      padding: "10px 12px",
      background: "var(--bg-subtle)",
      border: "1px dashed var(--border-strong)",
      borderRadius: "var(--radius)",
    }}>
      <div style={{ fontSize: 11.5, color: "var(--fg-muted)", marginBottom: 7, display: "flex", alignItems: "center", gap: 6 }}>
        <IconGauge size={12} />
        這個回答信心{confidence.level === "low" ? "偏低" : "中等"}，建議追問：
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {suggestions.map((s, i) => (
          <button key={i} onClick={() => onPick?.(s)} style={{
            padding: "5px 10px",
            fontSize: 12,
            background: "var(--bg-elev)",
            border: "1px solid var(--border)",
            borderRadius: 999,
            cursor: "pointer",
            color: "var(--fg)",
          }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--accent)"; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--border)"; }}>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
};

// ---- Audit watermark ----
export const AuditWatermark = ({ traceId, conversationId, latencyMs, timestamp }) => {
  const [copied, setCopied] = useState(false);
  if (!traceId) return null;
  const fullText = `trace: ${traceId} · conv: ${conversationId || "—"} · ${timestamp || "—"} · ${latencyMs || "—"}ms`;

  const copy = () => {
    navigator.clipboard?.writeText(fullText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  return (
    <button
      onClick={copy}
      title="點擊複製完整 audit 資訊"
      style={{
        display: "inline-flex", alignItems: "center", gap: 8,
        marginTop: 6, padding: "2px 0",
        background: "transparent", border: "none",
        fontFamily: "var(--font-mono)", fontSize: 10,
        color: "var(--fg-subtle)",
        cursor: "pointer",
        letterSpacing: 0.2,
      }}>
      <span>trace: {traceId}</span>
      <span style={{ opacity: 0.5 }}>·</span>
      {conversationId && <><span>conv: {String(conversationId).slice(0, 10)}</span><span style={{ opacity: 0.5 }}>·</span></>}
      {latencyMs != null && <><span>{latencyMs}ms</span><span style={{ opacity: 0.5 }}>·</span></>}
      <span style={{ color: copied ? "var(--success)" : "var(--fg-subtle)" }}>
        {copied ? "✓ copied" : "copy"}
      </span>
    </button>
  );
};

// ---- Classified / Confidential badges ----
export const ClassifiedCorner = () => (
  <div style={{
    position: "absolute", top: 0, right: 0,
    padding: "1px 7px",
    background: "var(--danger)",
    color: "white",
    fontSize: 8.5, fontWeight: 700, letterSpacing: 1,
    fontFamily: "var(--font-mono)",
    borderBottomLeftRadius: 4,
  }}>CONFIDENTIAL</div>
);

export const ConfidentialWatermark = ({ userEmail, traceId }) => (
  <div aria-hidden="true" style={{
    position: "fixed", inset: 0, pointerEvents: "none",
    zIndex: 4,
    opacity: 0.055,
    background: `repeating-linear-gradient(-30deg,
      transparent 0,
      transparent 140px,
      var(--fg) 140px,
      var(--fg) 141px,
      transparent 141px,
      transparent 280px)`,
    display: "flex", alignItems: "center", justifyContent: "center",
    overflow: "hidden",
  }}>
    <div style={{
      fontFamily: "var(--font-mono)", fontSize: 14,
      transform: "rotate(-20deg)",
      color: "var(--fg)",
      textAlign: "center",
    }}>
      CLASSIFIED · {userEmail || "user"} · {traceId || "—"}
    </div>
  </div>
);
