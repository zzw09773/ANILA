// Changelog "What's New" modal. Static, build-time content (air-gapped: no
// fetch). A subtle dot on the trigger marks unseen entries — tracked via the
// latest version vs a localStorage "last seen" stamp.
import React from "react";
import { IconX } from "./icons.jsx";

// Newest first. Bump CHANGELOG_VERSION when adding an entry so the "new" dot
// reappears for everyone until they open the modal.
//
// ⚠ 發佈紀律(W1-9):這份清單曾經凍在 2026-06-12 一個半月,其間三批使用者
// 看得到的功能上線,但**沒有任何人被通知** —— 機制活著、內容凍結。PR 模板
// (`.github/pull_request_template.md`)已加上「使用者可見變更已進 changelog?」
// 的 checklist;動到使用者看得到的東西就補一條並 bump 版本。
// 條目一律只寫**已在這條線上線**的東西;未合併分支的功能不得預告。
export const CHANGELOG_VERSION = "2026-07-26";

export const CHANGELOG = [
  {
    version: "2026-07-26",
    title: "說得準、找得到",
    items: [
      "頂欄原本寫「加密」的那個標記正名為「密等鎖定（latch）」—— 平台從未對訊息做靜態加密，舊字眼是錯的",
      "設定 → 記憶會明說這個部署有沒有開啟記憶功能，不再承諾未啟用的自動學習",
      "記憶功能關閉時仍可檢視與刪除既有紀錄（旗標不會沒收你的刪除權）",
      "新增「說明」入口與快捷鍵面板：按 ⌘/ 或 Ctrl+/ 開啟，輸入框也直接寫出來",
      "第一次登入會出現三步靜態導引卡（選 agent、上傳知識庫、提問）",
      "使用者說明文件初版 docs/guides/user-guide.md，含複製與匯出限制的「為什麼」",
    ],
  },
  {
    version: "2026-07-25",
    title: "穩定性與搜尋修正",
    items: [
      "修掉整站偶發卡住（後端 event-loop 連線死結）",
      "修掉登入後停在啟動畫面不動的情況",
      "對話搜尋改回「最近的 N 筆」，不再回任意 N 筆",
      "前端當機不再變成白畫面，改顯示可重試的錯誤區塊",
      "聊天一律改走 CSP proxy，權責與稽核路徑一致",
    ],
  },
  {
    version: "2026-07-20",
    title: "語音輸入與執行時間軸",
    items: [
      "輸入框支援語音輸入（ASR），知識庫端同步可用",
      "agent 執行過程有完整時間軸：工具呼叫、子任務與取消都看得到",
      "新增「產出中心」：跨知識庫的產出總覽，可深連結到單一產出",
    ],
  },
  {
    version: "2026-07-02",
    title: "密等分級與四大入口",
    items: [
      "浮水印改為真實分類等級，不再一律印 CONFIDENTIAL",
      "對話頂欄顯示五級密等徽章（無機密／營業秘密／機密／極機密／絕對機密）",
      "側欄改為四大入口：任務中心、我的知識庫、產出中心、專案入口",
      "專案入口可直接開啟已註冊的服務（iframe 或新分頁）",
      "回答可展開「執行軌跡」，看到 Router 與 agent 的實際呼叫樹",
    ],
  },
  {
    version: "2026-06-12",
    title: "對話體驗強化",
    items: [
      "輸入框支援注音/拼音選字，組字中按 Enter 不再誤送出",
      "回應產生中可按「停止」中止，保留已產生內容",
      "切換對話會保留未送出的草稿",
      "「重新產生」可選方向（更詳細 / 更簡潔 / 換個說法 / 自訂）",
      "回應被長度上限截斷時可一鍵「繼續產生」",
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
