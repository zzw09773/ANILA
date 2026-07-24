// Trust & transparency components (ESM)
import React, { useEffect, useMemo, useState } from "react";
import {
  IconBook, IconX, IconExternal, IconShield, IconGauge, IconLock,
  IconFile, IconLink, IconChevDown, IconChevRight,
} from "./icons.jsx";
import { IconButton } from "./components.jsx";
import { renderWithRedaction } from "./data.jsx";
import { classificationLevelBadge } from "./runtime/classified.js";
import {
  SCORE_FOOTNOTE,
  SCORE_LABEL,
  citationIdentity,
  groupCitations,
  normalizeCitation,
  scoreToPercent,
} from "./citationUtils.js";

// ---- Inline citation [N] ----
// Click behaviour unchanged: still passes the original citation object to onOpen.
export const CitationInline = ({ n, citation, onOpen }) => {
  const tip = citation
    ? (() => {
        const nrm = normalizeCitation(citation, Math.max(0, n - 1));
        const bits = [nrm.title];
        if (nrm.section) bits.push(nrm.section);
        const pct = scoreToPercent(nrm.score);
        if (pct != null) bits.push(`${SCORE_LABEL} ${pct}%`);
        return bits.join(" · ");
      })()
    : "";
  return (
    <button
      onClick={() => onOpen?.(citation)}
      title={tip}
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
};

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

// Compact classification pill for a single citation / group (五級; 無機密 不顯示).
const CitationClassBadge = ({ level }) => {
  const label = classificationLevelBadge({ classification_level: level });
  if (!label) return null;
  return (
    <span
      title={`分類等級：${label}`}
      style={{
        display: "inline-flex", alignItems: "center", gap: 3,
        padding: "1px 6px",
        background: "oklch(0.95 0.02 25 / 0.4)",
        border: "1px solid var(--danger)",
        borderRadius: 999,
        fontSize: 10, fontFamily: "var(--font-mono)",
        color: "var(--danger)",
        flexShrink: 0,
      }}
    >
      <IconLock size={9} />{label}
    </span>
  );
};

// Relevance percent + bar. Score is cosine similarity (higher = closer).
const RelevanceMeter = ({ score }) => {
  const pct = scoreToPercent(score);
  if (pct == null) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        marginBottom: 3,
      }}>
        <span style={{
          fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)",
        }}
          title={SCORE_FOOTNOTE}
        >{SCORE_LABEL}</span>
        <span style={{
          fontSize: 10, fontWeight: 600, color: "var(--fg-muted)",
          fontFamily: "var(--font-mono)",
        }}>{pct}%</span>
      </div>
      <div style={{
        height: 3, background: "var(--bg-subtle)",
        borderRadius: 999, overflow: "hidden",
      }}>
        <div style={{
          width: `${pct}%`, height: "100%",
          background: "var(--accent)",
        }} />
      </div>
    </div>
  );
};

const CitationChunkCard = ({ item, activeId, onJumpTo }) => {
  const active = activeId != null && String(activeId) === String(item.id);
  const raw = item.raw;
  return (
    <div
      data-cit-id={item.id}
      style={{
        padding: 10, marginBottom: 6,
        background: "var(--bg-elev)",
        border: "1px solid " + (active ? "var(--accent)" : "var(--border)"),
        borderRadius: "var(--radius)",
        boxShadow: active ? "0 0 0 3px oklch(0.58 0.08 200 / 0.2)" : "none",
        transition: "all .15s",
      }}
    >
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
          flexShrink: 0,
        }}>{item.n}</span>
        <div style={{
          fontSize: 12, fontWeight: 500, flex: 1,
          color: "var(--fg-muted)",
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>
          {item.section || `分片 ${item.n}`}
        </div>
        <CitationClassBadge level={item.classificationLevel} />
      </div>
      {item.snippet && (
        <div style={{
          padding: "7px 9px",
          background: "var(--bg-subtle)",
          borderLeft: "2px solid var(--border-strong)",
          fontSize: 12, lineHeight: 1.6, color: "var(--fg)",
          borderRadius: 3,
          marginBottom: 8,
          whiteSpace: "pre-wrap",
          maxHeight: 160, overflow: "auto",
        }}>{item.snippet}</div>
      )}
      <RelevanceMeter score={item.score} />
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {item.updatedAt && (
          <span style={{ fontSize: 10, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>
            updated {item.updatedAt}
          </span>
        )}
        <div style={{ flex: 1 }} />
        {item.sourceUri && (
          <button
            onClick={() => onJumpTo?.(raw)}
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
  );
};

const CitationGroup = ({ group, expanded, onToggle, activeId, onJumpTo }) => {
  const count = group.items.length;
  const Icon = group.kind === "url" ? IconLink : IconFile;
  // Single-chunk groups stay flat (no collapse chrome); multi-chunk collapse.
  const collapsible = count > 1;
  const showBody = !collapsible || expanded;

  return (
    <div style={{ marginBottom: 10 }}>
      <button
        type="button"
        onClick={collapsible ? onToggle : undefined}
        disabled={!collapsible}
        style={{
          display: "flex", alignItems: "center", gap: 6,
          width: "100%",
          padding: "6px 4px",
          background: "transparent",
          border: "none",
          cursor: collapsible ? "pointer" : "default",
          textAlign: "left",
          color: "var(--fg)",
        }}
      >
        {collapsible
          ? (expanded ? <IconChevDown size={12} /> : <IconChevRight size={12} />)
          : <span style={{ width: 12 }} />}
        <span style={{ color: "var(--fg-muted)", display: "inline-flex", flexShrink: 0 }}>
          <Icon size={12} />
        </span>
        <span style={{
          fontSize: 12.5, fontWeight: 600, flex: 1,
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>{group.title}</span>
        <span style={{
          fontFamily: "var(--font-mono)", fontSize: 10,
          color: "var(--fg-subtle)",
          padding: "1px 6px",
          background: "var(--bg)",
          border: "1px solid var(--border)",
          borderRadius: 999,
          flexShrink: 0,
        }}>{count} 分片</span>
        <CitationClassBadge level={group.classificationLevel} />
      </button>
      {showBody && group.items.map((item) => (
        <CitationChunkCard
          key={item.id}
          item={item}
          activeId={activeId}
          onJumpTo={onJumpTo}
        />
      ))}
    </div>
  );
};

// ---- Citations Drawer ----
export const CitationsDrawer = ({ open, citations, activeId, onClose, onJumpTo }) => {
  const grouped = useMemo(() => groupCitations(citations), [citations]);
  const docCount = useMemo(
    () => grouped.documents.filter((g) => g.kind === "document").length,
    [grouped],
  );
  const hasScore = useMemo(
    () => (Array.isArray(citations) ? citations : []).some((c) => typeof c?.score === "number"),
    [citations],
  );

  // Expand groups that contain the active citation; otherwise expand all.
  const [expandedKeys, setExpandedKeys] = useState(() => new Set());
  useEffect(() => {
    if (!open) return;
    const next = new Set();
    const activeStr = activeId != null ? String(activeId) : null;
    let hit = false;
    for (const g of grouped.documents) {
      if (g.items.length <= 1) {
        next.add(g.key);
        continue;
      }
      if (activeStr && g.items.some((it) => String(it.id) === activeStr)) {
        next.add(g.key);
        hit = true;
      }
    }
    if (!hit) {
      for (const g of grouped.documents) next.add(g.key);
    }
    setExpandedKeys(next);
  }, [open, activeId, grouped]);

  if (!open) return null;

  const toggle = (key) => {
    setExpandedKeys((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  };

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
        }}>{grouped.total} 筆{docCount > 0 ? ` · ${docCount} 份文件` : ""}</span>
        <div style={{ flex: 1 }} />
        <IconButton onClick={onClose}><IconX /></IconButton>
      </div>
      {hasScore && (
        <div style={{
          padding: "8px 14px",
          borderBottom: "1px solid var(--border)",
          fontSize: 10.5, lineHeight: 1.45,
          color: "var(--fg-subtle)",
          background: "var(--bg)",
        }}
          title={SCORE_FOOTNOTE}
        >{SCORE_FOOTNOTE}</div>
      )}
      <div style={{ flex: 1, overflowY: "auto", padding: "10px 12px 14px" }}>
        {grouped.documents.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--fg-muted)", padding: 8 }}>尚無引用來源</div>
        )}
        {grouped.documents.map((group) => (
          <CitationGroup
            key={group.key}
            group={group}
            expanded={expandedKeys.has(group.key)}
            onToggle={() => toggle(group.key)}
            activeId={activeId != null ? String(activeId) : null}
            onJumpTo={onJumpTo}
          />
        ))}
      </div>
    </div>
  );
};

// Re-export helpers so tests / callers can import from trust if needed.
export { citationIdentity, groupCitations, normalizeCitation, scoreToPercent };

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
export const AuditWatermark = ({ traceId, conversationId, latencyMs, timestamp, usage }) => {
  const [copied, setCopied] = useState(false);
  if (!traceId) return null;
  const tokenTotal = usage?.total_tokens || 0;
  const fullText = `trace: ${traceId} · conv: ${conversationId || "—"} · ${timestamp || "—"} · ${latencyMs || "—"}ms${tokenTotal ? ` · ${tokenTotal} tokens` : ""}`;

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
      {tokenTotal > 0 && <><span title={`prompt ${usage.prompt_tokens || 0} · completion ${usage.completion_tokens || 0}`}>{tokenTotal} tokens</span><span style={{ opacity: 0.5 }}>·</span></>}
      <span style={{ color: copied ? "var(--success)" : "var(--fg-subtle)" }}>
        {copied ? "✓ copied" : "copy"}
      </span>
    </button>
  );
};

// ---- Classification corner watermark (真五級,取代裝飾性英文 CONFIDENTIAL) ----
// 依實際 classification_level 渲染右上角標:僅「機密」(含)以上顯示,角標文字
// 恆為真級別中文(機密／極機密／絕對機密),絕不再顯示固定英文 CONFIDENTIAL。
// 營業秘密／無機密／未知等級不上全浮水印(營業秘密僅保留 ClassificationLevelBadge)。
// 視覺分級遞進:機密(warn 琥珀)→ 極機密(danger 紅)→ 絕對機密(danger 紅 + 加重)。
const WATERMARK_LEVEL_STYLES = {
  機密: { severity: "warn", bg: "var(--warn)" },
  極機密: { severity: "danger", bg: "var(--danger)" },
  絕對機密: { severity: "danger-strong", bg: "var(--danger)" },
};

/**
 * 由對話狀態推導要顯示的浮水印級別字串(zh-TW),無則回傳 null。
 * 優先讀 classificationLevel / classification_level;僅「機密／極機密／絕對機密」
 * 觸發全浮水印。欄位缺漏(僅有 boolean latch payload)但 classified 為真時,
 * 回退 floor「機密」(與 r1_0003 backfill 一致),絕不回退英文 CONFIDENTIAL。
 *
 * @param {{classificationLevel?: string, classification_level?: string, classified?: boolean}} source
 * @returns {string|null}
 */
export function watermarkLevel(source) {
  const raw = source?.classificationLevel ?? source?.classification_level;
  if (typeof raw === "string" && raw.trim() && WATERMARK_LEVEL_STYLES[raw.trim()]) {
    return raw.trim();
  }
  // 欄位缺漏或非機敏等級時,以 boolean latch 為準:classified=true → floor「機密」。
  return source?.classified ? "機密" : null;
}

export const ClassificationWatermark = ({ level }) => {
  const key = typeof level === "string" ? level.trim() : "";
  const style = WATERMARK_LEVEL_STYLES[key];
  if (!style) return null;
  const strong = style.severity === "danger-strong";
  return (
    <div
      data-classification={key}
      data-severity={style.severity}
      className={`anila-classification-watermark anila-classification-${style.severity}`}
      title={`此對話分類等級：${key}`}
      style={{
        position: "absolute", top: 0, right: 0,
        padding: strong ? "2px 8px" : "1px 7px",
        background: style.bg,
        color: "white",
        fontSize: 8.5, fontWeight: strong ? 800 : 700, letterSpacing: 1,
        fontFamily: "var(--font-mono)",
        borderBottomLeftRadius: 4,
        border: strong ? "1px solid white" : "none",
        boxShadow: strong ? "0 0 0 1px var(--danger)" : "none",
      }}
    >{key}</div>
  );
};

// Multi-level classification badge (Slice 3c). Renders the zh-TW level text in
// the SAME pill style family as the existing "加密模式" indicator, next to it.
// Returns null (renders nothing) when the conversation has no elevated level —
// either the field is absent (boolean-only latch payload) or it is the floor
// 無機密. The boolean 加密模式 indicator is rendered independently by app.jsx,
// so this badge is purely additive.
export const ClassificationLevelBadge = ({ conversation }) => {
  const label = classificationLevelBadge(conversation);
  if (!label) return null;
  return (
    <span
      title={`此對話分類等級：${label}`}
      style={{
        display: "inline-flex", alignItems: "center", gap: 4,
        padding: "3px 9px",
        background: "oklch(0.95 0.02 25 / 0.4)",
        border: "1px solid var(--danger)",
        borderRadius: 999,
        fontSize: 11, fontFamily: "var(--font-mono)",
        color: "var(--danger)",
      }}
    >
      <IconLock size={11} /> {label}
    </span>
  );
};

// 機敏模式全螢幕鑑識浮水印:低透明度對角平鋪,萬一有人拍照/截圖洩漏機敏畫面,
// 浮水印帶著洩漏者身分 + trace_id 以供溯源。文字顯示「真實分類級別」中文
// (機密/極機密/絕對機密),非固定英文;缺 level 回退機密(與 r1_0003 backfill 一致)。
export const ConfidentialWatermark = ({ userEmail, traceId, level }) => (
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
      {level || "機密"} · {userEmail || "user"} · {traceId || "—"}
    </div>
  </div>
);
