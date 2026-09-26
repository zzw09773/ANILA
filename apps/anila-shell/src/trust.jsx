// Trust & transparency components (ESM)
import React, { useState, useEffect } from "react";
import { IconBook, IconX, IconExternal, IconShield, IconGauge, IconLock } from "./icons.jsx";
import { IconButton } from "./components.jsx";
import { blockingHits, summarizePIIHits } from "./data.jsx";
import { classificationLevelBadge } from "./runtime/classified.js";
import { usageTokenTooltip } from "./runtime/usageDisplay.js";

// ---- Inline citation [N] ----
export const CitationInline = ({ n, citation, onOpen }) => (
  <button
    onClick={() => onOpen?.(citation)}
    // `section` 是選配的:院內規章的引用沒有這個欄位(定位點是條號,不是章節),
    // 而樣板字串會把缺席印成字面的 "undefined" —— 在一個使用者正拿來查證依據
    // 的表面上顯示 "undefined" 是最糟的一種。守衛沿用抽屜端既有的寫法(:107 的
    // `c.section &&`),讓同一份資料的兩個表面講同一句話。
    title={citation ? (citation.section ? `${citation.title} · ${citation.section}` : citation.title) : ""}
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

// ---- 敏感資訊提示列 ----

/**
 * 敏感資訊模式,由寬到嚴。單一真相來源:提示列的按鈕、設定頁的那組按鈕、
 * 以及 app.jsx 從 users.ui_settings 讀回偏好時的驗證,都讀這一份。
 *
 * 兩個模式做的都是它字面上的事:`warn` 告訴你草稿裡疑似有什麼,`block` 不讓它送出。
 *
 * ⚠ 順序有意義:`block` 對「離開瀏覽器的內容」最嚴(真的擋住送出),`warn` 最寬
 * (只顯示這條提示)。所以把使用者從自己選的模式改回預設,方向上是**放寬**保護,
 * 不是單純忘記一個偏好 —— 這就是它必須被存下來的理由。
 *
 * ⚠ 預設是 `warn`:兩個裡最寬的那個,只告知、不替使用者動手。
 * ⚠ `users.ui_settings` 裡可能還留著已經不存在的舊值。白名單驗證(app.jsx)
 *   會讓它落在預設上 —— 讀回不認得的值不是錯誤,不要在那裡噴 console。
 */
export const REDACTION_MODES = ["warn", "block"];
export const REDACTION_MODE_DEFAULT = "warn";

// 提示列的工作是讓人停一秒自己判斷,不是替他決定:說出**看到了什麼**、
// 以及**送出去之後會怎樣**,然後就閉嘴。不給指示、不代為動作。
//
// ⚠ 用「疑似」不用「有」。偵測器認的是**形狀**(身分證與信用卡另外驗檢查碼),
// 它並不知道那串字究竟是什麼。2026-08-06 以前的版本在十個常見院內樣本上
// 十個全誤報(採購案號、預算欄、年度欄、16 位料號、公文編號、承辦人 email…);
// 檢查碼與分隔符收窄之後那十個都不再命中,但「可能認錯」這件事沒有消失,
// 只是變少了。對著一張預算表斷言「這裡有 1 個信用卡」,錯一次使用者就再也
// 不看這條橫幅了。這是偵測→警示,不是偵測→斷定。
//
// ⚠ 同樣重要的是**反過來**那一句:它只認得清單上的那幾種形狀,沒被指出來的
// 個資與祕密照樣在草稿裡。這條橫幅沒出現不代表草稿是乾淨的。
export const RedactionHint = ({ hits, mode, onChangeMode }) => {
  if (!hits || hits.length === 0) return null;
  const summary = summarizePIIHits(hits);
  // ⚠ block 攔得住的只有 pii 那一類。憑證(API 金鑰、權杖、私密金鑰、密碼)
  // 是只警示的 —— 草稿裡只有一把 API 金鑰的時候,這條橫幅**不可以**寫
  // 「這則不會送出」,因為它送得出去。橫幅說了會發生的事就必須真的發生。
  const willBlock = mode === "block" && blockingHits(hits).length > 0;

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
        這則草稿裡疑似有 <b>{summary}</b>（只比對格式，可能認錯）。
        {willBlock
          ? "目前模式是 block，這則不會送出。"
          : mode === "block"
            ? "這幾種只提醒、不擋送出——送出後，模型和這份對話紀錄都會留著它，收不回來。"
            : "送出後，模型和這份對話紀錄都會留著它，收不回來。"}
      </span>
      <div style={{ flex: 1 }} />
      <div style={{ display: "flex", gap: 2 }}>
        {REDACTION_MODES.map(m => (
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
export const INJECTION_NOTICE = "參考資料中有疑似指令，已忽略";

export const AuditWatermark = ({ traceId, conversationId, latencyMs, timestamp, usage, promptInjectionSuspected }) => {
  const [copied, setCopied] = useState(false);
  if (!traceId && !promptInjectionSuspected) return null;
  const tokenTotal = usage?.total_tokens || 0;
  const fullText = `trace: ${traceId} · conv: ${conversationId || "—"} · ${timestamp || "—"} · ${latencyMs || "—"}ms${tokenTotal ? ` · ${tokenTotal} tokens` : ""}`;

  const copy = () => {
    navigator.clipboard?.writeText(fullText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  return (
    <details style={{ marginTop: 6 }}><summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--fg-muted)' }}>回覆詳情</summary>
    {promptInjectionSuspected ? (
      <div data-injection-notice="true" style={{ fontSize: 12, color: "var(--fg-muted)", marginTop: 4 }}>{INJECTION_NOTICE}</div>
    ) : null}
    {traceId ? <button
      onClick={copy}
      className="anila-audit-watermark"
      title="複製追蹤資訊"
      style={{
        display: "inline-flex", alignItems: "center", gap: 8, flexWrap: "wrap",
        maxWidth: "100%",
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
      {tokenTotal > 0 && <><span title={usageTokenTooltip(usage)}>{tokenTotal} tokens</span><span style={{ opacity: 0.5 }}>·</span></>}
      <span style={{ color: copied ? "var(--success)" : "var(--fg-subtle)" }}>
        {copied ? "已複製" : "複製"}
      </span>
    </button> : null}</details>
  );
};

// ---- Classification corner watermark (真四級,取代裝飾性英文 CONFIDENTIAL) ----
// 依實際 classification_level 渲染右上角標:僅「密」(含)以上顯示,角標文字
// 恆為真級別中文(密／機密),絕不再顯示固定英文 CONFIDENTIAL。
// 營業秘密／無機密／未知等級不上全浮水印(營業秘密僅保留 ClassificationLevelBadge)。
// 視覺分級遞進:密(warn 琥珀,舊 rank-2 機密角色)→ 機密(danger-strong 頂級)。
const WATERMARK_LEVEL_STYLES = {
  密: { severity: "warn", bg: "var(--warn)" },
  機密: { severity: "danger-strong", bg: "var(--danger)" },
};

/**
 * 由對話狀態推導要顯示的浮水印級別字串(zh-TW),無則回傳 null。
 * 優先讀 classificationLevel / classification_level;僅「密／機密」
 * 觸發全浮水印。欄位缺漏(僅有 boolean latch payload)但 classified 為真時,
 * 回退 floor「密」(鏡射門檻 >= 密;classified=true 只保證 >= 密),
 * 絕不回退英文 CONFIDENTIAL,也絕不回退更高的「機密」。
 *
 * @param {{classificationLevel?: string, classification_level?: string, classified?: boolean}} source
 * @returns {string|null}
 */
export function watermarkLevel(source) {
  const raw = source?.classificationLevel ?? source?.classification_level;
  if (typeof raw === "string" && raw.trim() && WATERMARK_LEVEL_STYLES[raw.trim()]) {
    return raw.trim();
  }
  // 欄位缺漏或非機敏等級時,以 boolean latch 為準:classified=true → floor「密」。
  return source?.classified ? "密" : null;
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

/**
 * Format a reading timestamp to minute precision (local clock).
 * Intent: useful on a screenshot taken minutes later, not a live-updating clock.
 * @param {Date} [date]
 * @returns {string} e.g. "2026-07-30 15:30"
 */
export function formatWatermarkMinute(date = new Date()) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}`;
}

/**
 * Resolve the reader label from the signed-in user object the shell already holds.
 * Prefers email, then username. Returns null when identity is not yet known —
 * never invents a placeholder (empty string / "user") that would mis-attribute a leak.
 *
 * @param {{email?: string, username?: string} | null | undefined} user
 * @returns {string|null}
 */
export function watermarkReaderLabel(user) {
  if (!user || typeof user !== "object") return null;
  for (const key of ["email", "username"]) {
    const raw = user[key];
    if (typeof raw === "string" && raw.trim()) return raw.trim();
  }
  return null;
}

/**
 * Build the forensic watermark line: level · reader · time.
 * Spec (SYSTEM-MAP §8 / PLAN P4.2):「本文件屬{密等} · 讀取者 · 時間」.
 *
 * @param {{level: string, reader: string, readAt: string}} parts
 * @returns {string}
 */
export function buildWatermarkText({ level, reader, readAt }) {
  return `本文件屬${level} · ${reader} · ${readAt}`;
}

/**
 * Short honest caption shown next to the controlled-conversation indicator
 * (列管模式 / ClassificationLevelBadge) whenever the full-page mark is on.
 * Tracing purpose only — never claims to prevent copy / screenshot / print.
 */
export const WATERMARK_DISCLAIMER =
  "浮水印供外洩溯源，不阻止複製、截圖或列印。";

// 機敏模式全螢幕鑑識浮水印:低透明度對角平鋪。萬一有人拍照/截圖洩漏機敏畫面,
// 浮水印帶著密等 + 讀取者 + 讀取時間(分鐘精度)以供溯源。
// 門檻與 ClassificationWatermark / watermarkLevel 相同:僅「密／機密」。
// 讀取者身分尚未就緒時不渲染 —— 絕不回退空白或 "user" 等誤導字樣。
// pointer-events:none,不阻擋底下選取。
// 讀取時間對「目前顯示的對話」凍結:換對話才重凍,同對話保持穩定。
const WATERMARK_TILE_COUNT = 36;

/**
 * @param {{
 *   level: string,
 *   reader?: string,
 *   userEmail?: string,
 *   conversationId?: string|number|null,
 *   readAtForTests?: string,
 * }} props
 *
 * `readAtForTests` is a **TEST SEAM ONLY**. Production call sites must omit it
 * so the stamp comes from the local clock at conversation open. Do not wire
 * this prop to API fields, message metadata, or any other runtime source.
 */
export const ConfidentialWatermark = ({
  level,
  reader,
  userEmail,
  conversationId,
  // TEST SEAM ONLY — see JSDoc above. Never pass from production.
  readAtForTests,
}) => {
  const testStamp =
    typeof readAtForTests === "string" && readAtForTests.trim()
      ? readAtForTests.trim()
      : null;

  const [frozenAt, setFrozenAt] = useState(
    () => testStamp ?? formatWatermarkMinute()
  );

  // Re-freeze when the displayed conversation changes; keep stable while it stays open.
  useEffect(() => {
    if (testStamp) {
      setFrozenAt(testStamp);
      return;
    }
    setFrozenAt(formatWatermarkMinute());
  }, [conversationId, testStamp]);

  const key = typeof level === "string" ? level.trim() : "";
  if (!WATERMARK_LEVEL_STYLES[key]) return null;

  // Prefer explicit `reader`; accept legacy `userEmail` prop name from older call sites.
  // Both are plain strings from the signed-in user — never invent a placeholder.
  const rawReader = typeof reader === "string" ? reader : userEmail;
  const readerLabel =
    typeof rawReader === "string" && rawReader.trim() ? rawReader.trim() : null;
  if (!readerLabel) return null;

  const stamp = testStamp ?? frozenAt;
  const text = buildWatermarkText({ level: key, reader: readerLabel, readAt: stamp });

  return (
    <div
      aria-hidden="true"
      data-classification={key}
      data-watermark="forensic"
      data-watermark-conversation={conversationId == null ? undefined : String(conversationId)}
      style={{
        position: "fixed", inset: 0, pointerEvents: "none",
        zIndex: 4,
        overflow: "hidden",
        // Cover the viewport while scrolling; tiles keep identity on any crop.
        display: "grid",
        gridTemplateColumns: "repeat(3, 1fr)",
        gridTemplateRows: "repeat(12, 1fr)",
        alignItems: "center",
        justifyItems: "center",
        gap: 0,
        userSelect: "none",
      }}
    >
      {Array.from({ length: WATERMARK_TILE_COUNT }, (_, i) => (
        <div
          key={i}
          data-watermark-tile=""
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            fontWeight: 600,
            letterSpacing: 0.3,
            transform: "rotate(-28deg)",
            whiteSpace: "nowrap",
            // 顏色必須跟著主題走。曾經寫死深灰字 + 白色外光暈,淺色主題正常,
            // 深色主題下白光暈變成畫面最亮的東西,整片浮水印蓋過對話內容
            // (2026-07-30 目視實測)。字用前景色、光暈用背景色,兩個主題都是
            // 「比內容淡一階」而不是「反白」。
            color: "color-mix(in oklab, var(--fg) 16%, transparent)",
            textShadow:
              "0 0 1px color-mix(in oklab, var(--bg) 70%, transparent), 0 1px 0 color-mix(in oklab, var(--bg) 45%, transparent)",
            textAlign: "center",
            lineHeight: 1.2,
          }}
        >
          {text}
        </div>
      ))}
    </div>
  );
};
