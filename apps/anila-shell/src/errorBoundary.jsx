// 頂層錯誤邊界 —— W0-7(補救計畫 Wave 0)。
//
// 為什麼需要:本 SPA 在此之前對 `ErrorBoundary`/`componentDidCatch`/
// `getDerivedStateFromError` 三個關鍵字的命中數是 0,任何 render 期 throw
// 就是白畫面 + 零訊息 + 零復原路徑,而這條路徑覆蓋數百名使用者的日常聊天。
//
// 設計取捨(刻意與 anilalm 舊版相反):
//   1. **不把 stack / component stack 渲染給使用者看**。涉密平台向一般使用者
//      洩漏內部檔案路徑與元件名沒有好處,而且 error.message 在資料渲染元件
//      崩潰時可能夾帶使用者內容或文件內文。完整資訊一律走 console。
//   2. 改為顯示一個**短錯誤代碼**。使用者把代碼報給管理員,管理員在
//      console/log 以同一代碼 grep 回原始錯誤。代碼是 (name, message) 的
//      決定性函式 → 同一個 bug 永遠同一個代碼,可聚合。
//   3. 顏色一律走 design token(`--fg` / `--bg` / `--danger` …),不寫死 hex。
//      anilalm 舊版寫死 `#0B0D10`/`#E8EAED` 導致淺色主題下完全不對盤。
//
// 未涵蓋(刻意):訊息列表層的細粒度 boundary(單則訊息炸不掀全頁)需要動
// `chat.jsx`,那支檔案在 PR #50 的變更集內,留待該 PR 收斂後再做。

import React from "react";

/**
 * (name, message) → 6 碼 base36 短代碼。決定性:同一個錯誤永遠同一個代碼。
 * 不是密碼學雜湊,只求穩定與可 grep。
 * @param {string} input
 * @returns {string}
 */
export function errorCode(input) {
  let h = 0x811c9dc5; // FNV-1a offset basis
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    // FNV prime,用 Math.imul 保持 32-bit 環繞語意
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(36).toUpperCase().padStart(6, "0").slice(-6);
}

const WRAP = {
  minHeight: "100dvh",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 24,
  background: "var(--bg, #f6f7f9)",
  color: "var(--fg, #1a2029)",
};

const CARD = {
  maxWidth: 520,
  width: "100%",
  border: "1px solid var(--border, #dbdee2)",
  borderRadius: "var(--anila-radius-lg, 12px)",
  background: "var(--bg-elev, #ffffff)",
  padding: 24,
};

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, code: null };
  }

  static getDerivedStateFromError(error) {
    const signature = `${error?.name || "Error"}:${error?.message || ""}`;
    return { error, code: errorCode(signature) };
  }

  componentDidCatch(error, info) {
    // 完整資訊只進 console —— 使用者面只給代碼。W3-3 的 X-Request-ID 上線後
    // 這裡要一併把 request id 帶進上報。
    const code = this.state.code || errorCode(`${error?.name}:${error?.message}`);
    console.error(`[ANILA crash ${code}]`, error);
    console.error(`[ANILA crash ${code}] component stack:`, info?.componentStack);
  }

  handleReload = () => {
    // 不清 localStorage —— 三個 SPA 同源,清掉會誤傷 anilalm 與 governance
    // (anilalm 舊版 `localStorage.clear()` 就是這個 bug)。單純重載。
    window.location.reload();
  };

  render() {
    const { error, code } = this.state;
    if (!error) return this.props.children;

    return (
      <div style={WRAP} role="alert">
        <div style={CARD}>
          <h1 style={{ fontSize: 18, margin: "0 0 8px", color: "var(--danger, #ba3630)" }}>
            這個畫面發生錯誤
          </h1>
          <p style={{ fontSize: 14, lineHeight: 1.7, margin: "0 0 16px", color: "var(--fg-muted, #555b65)" }}>
            你的對話內容沒有遺失。請先重新載入;若同一個畫面重複發生,請把下面的
            錯誤代碼提供給平台管理員。
          </p>
          <div
            style={{
              fontFamily: "var(--font-mono, ui-monospace, monospace)",
              fontSize: 13,
              padding: "8px 12px",
              borderRadius: 8,
              background: "var(--bg-subtle, #eef0f3)",
              border: "1px solid var(--border, #dbdee2)",
              marginBottom: 16,
            }}
          >
            錯誤代碼:<strong>{code}</strong>
          </div>
          <button
            type="button"
            onClick={this.handleReload}
            style={{
              padding: "8px 16px",
              borderRadius: 8,
              border: "1px solid var(--accent, #2b4c7e)",
              background: "var(--accent, #2b4c7e)",
              color: "#ffffff",
              fontSize: 14,
              cursor: "pointer",
            }}
          >
            重新載入
          </button>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
