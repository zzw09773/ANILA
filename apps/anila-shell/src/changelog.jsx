// Changelog "What's New" modal. Static, build-time content (air-gapped: no
// fetch). A subtle dot on the trigger marks unseen entries — tracked via the
// latest version vs a localStorage "last seen" stamp.
import React from "react";
import { IconX } from "./icons.jsx";

// Newest first. Bump CHANGELOG_VERSION when adding an entry so the "new" dot
// reappears for everyone until they open the modal.
export const CHANGELOG_VERSION = "2026-06-12";

export const CHANGELOG = [
  {
    version: "2026-06-12",
    title: "對話體驗強化",
    items: [
      "輸入框支援注音/拼音選字，組字中按 Enter 不再誤送出",
      "回應產生中可按「停止」中止，保留已產生內容",
      "切換對話會保留未送出的草稿",
      "「重新產生」可選方向（更詳細 / 更簡潔 / 換個說法 / 自訂）",
      "回應被長度上限截斷時可一鍵「繼續」",
      "支援 Mermaid 流程圖 / 架構圖渲染",
      "可直接把檔案拖放到輸入框附加",
      "側邊對話列表依時間分組（今天 / 昨天 / 前 7 天 / 更早）",
    ],
  },
  {
    version: "2026-06-11",
    title: "Agent 功能與動作",
    items: [
      "開發者可在主控台為每個 agent 設計功能（預設提示詞 / 回應動作），不同專案客製",
      "對話時可點清單帶入該 agent 的預設提示詞",
      "回應旁提供一鍵動作（翻譯 / 摘要 / 改寫公文等）",
    ],
  },
];

export function ChangelogModal({ open, onClose }) {
  if (!open) return null;
  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, zIndex: 200,
        background: "oklch(0.10 0 0 / 0.45)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(560px, 100%)", maxHeight: "80vh", overflowY: "auto",
          background: "var(--bg-elev)", border: "1px solid var(--border)",
          borderRadius: "var(--radius-lg)", boxShadow: "0 16px 48px -12px oklch(0.10 0 0 / 0.3)",
        }}
      >
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "14px 18px", borderBottom: "1px solid var(--border)",
          position: "sticky", top: 0, background: "var(--bg-elev)",
        }}>
          <span style={{ fontSize: 15, fontWeight: 600 }}>新功能</span>
          <button onClick={onClose} aria-label="關閉" style={{
            display: "inline-flex", background: "transparent", border: "none",
            cursor: "pointer", color: "var(--fg-muted)", padding: 4,
          }}><IconX size={16} /></button>
        </div>
        <div style={{ padding: 18 }}>
          {CHANGELOG.map((release) => (
            <div key={release.version} style={{ marginBottom: 20 }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 600 }}>{release.title}</span>
                <span style={{ fontSize: 11, color: "var(--fg-subtle)", fontFamily: "var(--font-mono)" }}>{release.version}</span>
              </div>
              <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
                {release.items.map((it, i) => (
                  <li key={i} style={{ fontSize: 13, color: "var(--fg)", lineHeight: 1.5 }}>{it}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
