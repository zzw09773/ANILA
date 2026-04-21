# ANILA UI — Trust, Collaboration & Workflow Plan

> 聚焦 runtime 層面（end-user client）的可信度、協作與 workflow 能力。  
> 控制面（使用者/權限/API Key）仍在 myCSPPlatform，不重做；本計畫只補 runtime 缺失的使用者體驗模組。

---

## Scope

此計畫分為三個主題，共 10 項功能：

| # | 主題 | 功能 | 優先序 |
|---|------|------|--------|
| 1 | 信任與透明度 | 引用來源面板 | P0 |
| 2 | 信任與透明度 | 敏感資訊 redaction 提示 | P0 |
| 3 | 信任與透明度 | 置信度指示 | P1 |
| 4 | 信任與透明度 | 稽核浮水印（conversation-id / trace-id）| P0 |
| 5 | 信任與透明度 | 管制模式（機密對話） | P1 |
| 6 | 協作 | 對話分享與交接 | P2 |
| 7 | 協作 | 對話資料夾 / 標籤 | P2 |
| 8 | Multi-agent | 平行比較模式 | P1 |
| 9 | Multi-agent | Agent 交接視覺化 | P1 |
| 10 | Multi-agent | @ 提及切換 | P2 |
| 11 | Workflow | Onyx future module runtime UX | Future |

---

## 1. 引用來源面板（Citations Drawer）

### 目標
RAG agent 回覆時，user 可看到回答依據的文件片段、章節、最後更新時間，並跳到原文。回答旁出現上標 `[1]`, `[2]` 可點擊。

### 資料契約
後端（RAG agent）在 assistant message 回傳時附帶 `citations` 欄位（走 OpenAI 相容 extension）：

```json
{
  "message": { "role": "assistant", "content": "...[1]..." },
  "anila_meta": {
    "citations": [
      {
        "id": "cit-1",
        "title": "員工手冊 v2.3",
        "section": "第 4 章 特休",
        "snippet": "到職滿 6 個月未滿 1 年者，給予 3 日特別休假...",
        "source_uri": "doc://hr-handbook/v2.3#ch4-7",
        "updated_at": "2026-03-15",
        "score": 0.87
      }
    ]
  }
}
```

### UI
- 回答內文以上標 `[1]` 表示，hover 顯示 tooltip（標題+摘要），click 開啟右側抽屜並 scroll 到對應片段
- 主對話區右側新增一個**可收合抽屜**（預設收起），width 360px
- 抽屜 header：`來源 · N 筆` + 關閉鈕
- 每張來源卡片：標題、章節 path、相關性分數條、更新時間、片段（摘錄 80 字）、「開啟原文」按鈕
- 無 citation 時抽屜自動隱藏；有 citation 但未點擊時，標題列顯示小 badge 提示

### 元件
- `src/trust/CitationsDrawer.jsx`
- `src/trust/CitationInline.jsx`（上標+tooltip）
- message schema 新增 `citations` 欄位

---

## 2. 敏感資訊 Redaction 提示

### 目標
user 在輸入框鍵入疑似 PII 時，在送出前提示哪些片段會被自動遮罩，並在送出後標示「已對 LLM 遮罩」。

### 偵測規則（前端輕量 regex；真正 redaction 由 CSP 做）
| 類型 | Regex (簡化) |
|------|------|
| 身分證 | `/\b[A-Z]\d{9}\b/` |
| 手機 | `/\b09\d{2}-?\d{3}-?\d{3}\b/` |
| Email | `/[\w.+-]+@[\w-]+\.[\w.-]+/` |
| 信用卡 | `/\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b/` |

**注意**：前端偵測只是 UX 提示，**真正遮罩必須由 CSP proxy 執行並記錄**，避免繞過。

### UI
- Composer 下方出現 **橘色提示帶**：`偵測到 2 個敏感片段，送出時將自動遮罩：A12****678 · 09**-***-456`
- 送出後，user message bubble 內被遮罩處以 **等寬灰底** 顯示：`我的電話是 [REDACTED:PHONE]`，hover 顯示「已於 CSP 層遮罩，LLM 未接觸原值」
- 設定頁新增開關：「偵測到敏感資訊時 → [阻擋 | 警告 | 自動遮罩]」

### 元件
- `src/trust/RedactionDetector.jsx`
- `src/trust/RedactedSpan.jsx`
- 新 config：`tweaks.redactionMode`

---

## 3. 置信度指示

### 目標
agent 回覆旁顯示 low / medium / high confidence；低信心時自動建議 follow-up 問題或改用其他 agent。

### 資料來源
後端在 `anila_meta` 附 `confidence`：

```json
"anila_meta": {
  "confidence": { "level": "medium", "score": 0.62, "reasons": ["doc_coverage_partial", "entity_ambiguity"] },
  "suggested_follow_ups": ["你是指 2025 版還是 2026 版規定？"]
}
```

### UI
- Assistant header 旁加一個小 chip：`● 高信心` / `◐ 中等` / `○ 低信心`，顏色分別 success / warn / subtle
- 低信心時，回覆下方出現卡片：「這個回答信心偏低，建議追問：」+ 2-3 個 chip 按鈕，點擊直接送出
- 極低（<0.4）時在 routing trace 顯示 `degraded_confidence` 狀態節點

### 元件
- `src/trust/ConfidenceChip.jsx`
- `src/trust/FollowUpSuggestions.jsx`

---

## 4. 稽核浮水印（Conversation ID / Trace ID）

### 目標
user 遇到問題時能提供 ID 給管理員查 CSP audit log；不干擾日常閱讀。

### UI
- 每個 assistant message 下方（actions row 旁邊）小字：
  ```
  trace: tr_7f3a2b1c · conv: cv_0194 · 2026-04-20 14:32 · 142ms
  ```
  文字用 mono，色 `--fg-subtle`，size 10px
- 點擊可複製完整 ID。長按/右鍵顯示「回報問題」→ 生成預填 email 草稿含所有 meta
- 設定頁「隱私」新增開關：隱藏 trace ID 顯示（但仍保留在複製資訊中）

### 元件
- `src/trust/AuditWatermark.jsx`
- message schema 新增 `trace_id`, `conversation_id`, `latency_ms`

---

## 5. 管制模式（Classified Conversations）

### 目標
部分對話可被標記為「含機密」，強制關閉複製 / 匯出 / 分享功能；所有互動寫 audit。

### UI
- 對話 header 旁加一顆 **切換鎖**：🔓 / 🔒，開啟後：
  - message bubble 右上角顯示 `CONFIDENTIAL` 條紋角標
  - 頁面右下浮水印：`{user.email} · trace-id · classified`（防截圖追責）
  - 禁用：複製按鈕、下載、分享、右鍵選單
  - Composer 送出前彈 confirm：「此對話為機密，內容將留存稽核紀錄」
- 側邊欄該對話標題前加 🔒 icon
- 一旦設為機密 **無法降級**（只能由 admin 在 CSP 控制面解除）

### 後端
- message 增 `classification: "standard" | "confidential"` 欄位
- CSP audit log 寫入每次 view / reply 動作（與既有 audit_logs 表整合）

---

## 6. 對話分享與交接

### 目標
- **唯讀分享連結**：生成可給同事查看的 URL（限組織內、可設定過期）
- **對話 handoff**：把整個對話交接給另一個 agent 或另一個 user 續聊

### UI — 分享
- 對話 header overflow menu 新增「分享」→ 彈窗：
  - 生成連結 `https://anila.../s/c/7f3a2b1c`
  - 設定：過期時間（1h / 24h / 7d / never）、唯讀 or 允許 fork、組織內 or 指定人
  - 顯示此連結的 view log
- 被分享者開啟後看到 banner：`這是 alice.chen 在 2026-04-20 分享的對話（唯讀）`+「Fork 到我的空間」按鈕

### UI — Handoff
- overflow menu「交接給 agent / 同事」
- 給 agent：選另一個 agent，系統產生 handoff message `[Router] 從 hr-policy 交接給 finance-qa`，繼承上下文
- 給人：輸入帳號，對方側邊欄出現通知，accepted 後對話複製一份到對方空間

---

## 7. 對話資料夾 / 標籤

### 目標
跨專案歸檔，快速篩選。

### UI
- 側邊欄對話 tab 頂部新增「資料夾」section：`All · Starred · HR · Finance · Engineering`
- 對話 overflow menu：「加入資料夾」/「加標籤」
- 標籤顯示為 chips，多標籤可共存
- 搜尋列支援 `tag:hr q:特休` 語法

### 資料模型
- 前端：localStorage 儲存 user 個人資料夾（或寫回 CSP 的 user prefs）
- 標籤：`conversations.tags: string[]`

---

## 8. 平行比較模式（Parallel Compare）

### 目標
同一問題同時派給 2（或 3）個 agent，左右對照結果。最常用於「同一問題，不同 agent 觀點」的評估。

### UI
- 頂部 agent selector 旁新增「比較模式」切換 → 進入後 chat 畫面變 2 欄或 3 欄
- 每欄頂部有自己的 agent selector
- Composer 只有一個（底部），送出後訊息同時發到每個 column 的 agent
- 每欄獨立 streaming、獨立 trace；可在 column header「採用此回答」→ 剩餘欄隱藏變回正常對話
- 退出比較模式：只保留被採用的那一欄，或合併兩欄（產出新對話）

### 實作要點
- 每欄維護獨立 `messagesByConv[columnId]`
- 送出時並行呼叫多個 `dispatch_to_agent`
- 支援 row-level hover highlight（同時顯示兩欄 diff）

---

## 9. Agent 交接視覺化

### 目標
當 Router 中途 handoff（HR → Finance）時，在對話流顯示節點圖，讓 user 知道經過哪幾個 agent。

### UI
- routing trace 升級為可展開的 **水平 timeline**：
  ```
  [router] ── [hr-policy] ── [finance-qa] ── [done]
     42ms        830ms          1.2s           ✓
  ```
- 節點可點擊顯示該 agent 的輸入 / 輸出摘要（不含敏感內部 prompt）
- 多步驟對話時，整個對話可視圖會累積顯示節點流
- 若某 agent 呼叫失敗或被中斷，節點顯示紅色並附錯誤摘要

### 後端
- `anila_meta.handoff_chain: [{ agent_id, input_summary, output_summary, latency_ms }]`

---

## 10. @ 提及切換

### 目標
不離開輸入框快速切換 agent，提升重度 user 效率。

### UI
- 在 Composer 輸入 `@` 時彈出 agent dropdown（複用 AgentSelector）
- 選中後文本變成不可刪除的 chip `@rag-agent`，送出時該訊息 bypass router 直接指定
- 可多 agent：`@code-assist 幫我寫 SQL，@rag-agent 幫我查 schema 文件` → 平行送到兩個 agent（產生一個小型 parallel compare）

### 實作
- Composer 用 contenteditable 或自訂 token parser
- 送出 payload: `{ content: "...", explicit_agents: ["code-assist", "rag-agent"] }`

---

## 11. Onyx Workflow Future Module — Runtime UX Plan

> 對應 `docs/onyx-application-plan.md`，這裡定義當 Onyx 就緒時，ANILA UI 該如何呈現 workflow 類對話。

### 情境
user 輸入「我要請下週三一天假」。Router 判斷屬 workflow 類，dispatch 給 Onyx。Onyx 回傳的不再是純文字，而是 **結構化 workflow state**。

### 新 message 類型：`workflow_step`
```json
{
  "role": "assistant",
  "kind": "workflow_step",
  "workflow": {
    "id": "wf_leave_7a2b",
    "name": "請假申請",
    "state": "collecting" | "confirming" | "submitting" | "done" | "failed",
    "progress": { "current": 3, "total": 5, "label": "補齊欄位" },
    "form_schema": { ... JSONSchema ... },
    "form_values": { "leave_type": "annual", "date": null, "reason": null },
    "missing_fields": ["date", "reason"],
    "actions": [
      { "kind": "submit", "label": "送出申請", "confirmation_required": true },
      { "kind": "cancel", "label": "取消" }
    ]
  }
}
```

### UI 元件設計

**A. Workflow 狀態條（Progress Header）**
- 對話流頂部 sticky card：`請假申請 · 步驟 3/5 · 補齊欄位`
- 橫向進度條 + 狀態 pill
- 可隨時「取消 workflow」

**B. 內嵌結構化表單（Form Panel）**
- 取代純文字對答：右側或 inline 出現 form panel
- 依 form_schema 動態生成欄位（date picker / select / textarea / file upload）
- 已填的欄位 LLM 建議值標示為 `suggested`，user 確認後變 `confirmed`
- 缺欄位紅色邊框 + 提示文字

**C. 最終確認卡片（Confirmation Card）**
```
┌─────────────────────────────────────┐
│ ⚠ 即將送出：請假申請                  │
├─────────────────────────────────────┤
│ 類型：特休                           │
│ 日期：2026-04-24（週三）             │
│ 事由：家中事務                       │
│ 代理人：bob.lin                      │
├─────────────────────────────────────┤
│ 送出後無法直接修改，須經主管退件重送。  │
│                                     │
│ [ 取消 ]         [ 確認送出 ]       │
└─────────────────────────────────────┘
```
- mutating action 必須經此卡片；user 點「確認送出」才觸發真正的 API
- 卡片上顯示要打的目標系統 endpoint（透明度）

**D. 執行狀態與結果（Execution Timeline）**
- 送出後對話流出現節點：
  ```
  [submitted] ─ [manager_notify] ─ [status: pending]
  ```
- Onyx 回傳 `job_id` + polling schedule；UI 自動 poll 顯示狀態更新
- 失敗時顯示錯誤碼、retry 選項、rollback 狀態

**E. 規則輔助說明（Policy Hints）**
- 當 Onyx 查到相關規定時，在 form panel 側邊顯示：
  `💡 依勞基法 38 條，你今年還有 8 天特休未休，可在 12/31 前申請。`
- hint 與 deterministic check 分離顯示（hint = 參考；deterministic rule violation = 紅色阻擋）

### 互動流程範例

1. user：「我要請下週三一天假」
2. Onyx 建立 workflow，回傳 `state=collecting`，form 已預填 `leave_type=annual, date=2026-04-24`，缺 `reason`
3. UI 顯示 progress header + form panel；reason 欄位紅框
4. user 在 form panel 填「家中事務」→ 送出
5. Onyx 回 `state=confirming` + 最終確認卡片
6. user 點「確認送出」→ Onyx 呼叫 EIP submit API → 回 `state=submitting` + `job_id`
7. UI 輪詢 → `state=done`，顯示申請單號、主管通知狀態
8. 整個 workflow 在對話歷史中以特殊 `workflow` tag 標示，可在「資料夾 / 標籤」中篩選

### 責任邊界（呼應 Onyx plan）
- UI **不做** 規則判定；所有 validation 都由 Onyx + 目標系統決定
- UI **必須做** 的事：
  - 最終確認卡片（防止 LLM 誤送）
  - 送出後不可直接 retract（避免 user 誤以為可回退）
  - 顯示 trace_id 以便稽核
  - 失敗狀態清楚傳達給 user（不只是「失敗了」，要有錯誤碼 + 下一步建議）

### 元件清單
- `src/workflow/WorkflowProgress.jsx`
- `src/workflow/WorkflowForm.jsx` (schema-driven)
- `src/workflow/ConfirmationCard.jsx`
- `src/workflow/ExecutionTimeline.jsx`
- `src/workflow/PolicyHint.jsx`
- message kind 擴充：`workflow_step`

### 啟動前提（對齊 Onyx plan 的 Start Criteria）
- 至少一個代表性內部系統 API（EIP 請假系統）就緒
- Onyx 的 workflow state schema 定稿
- 最終確認卡片的 confirmation token 機制（防 replay）定義完成
- audit correlation（workflow_id ↔ target_system_ticket_id）串起來

Onyx 未就緒前，本模組保持 design-only，不實作。

---

## Build Sequence

### Wave 1（2-3 週）— 信任核心
1. 引用來源面板（#1）
2. 稽核浮水印（#4）
3. 敏感資訊 redaction 提示（#2）

這三個一起上線，讓 ANILA 從「另一個 chat UI」升級成「企業可用的 AI runtime」。

### Wave 2（2 週）— Multi-agent 體驗
4. 置信度指示（#3）
5. Agent 交接視覺化（#9）
6. 平行比較模式（#8）

### Wave 3（2 週）— 協作與管制
7. 管制模式（#5）
8. 對話資料夾 / 標籤（#7）
9. 對話分享與交接（#6）

### Wave 4（1 週）— 效率
10. @ 提及切換（#10）

### Wave 5（待 Onyx 就緒）— Workflow
11. Onyx Workflow runtime UX（#11）

---

## 設計原則

1. **透明大於聰明**：寧可多一行 `trace: tr_...` 也不要讓 user 質疑「這是怎麼算出來的」
2. **管制動作必經確認**：任何會寫入外部系統的動作（mutating action），UI 必須有獨立確認步驟，不可由 LLM 直接觸發
3. **failure 要講清楚**：不只是「失敗了」，要顯示錯誤碼、下一步建議、是否可重試
4. **不重複 CSP 控制面**：user / permission / API key CRUD 一律連回 myCSPPlatform 控制面，ANILA UI 不自建
5. **audit 永遠在線**：即使在管制模式，user 自己也看得到 trace_id，讓「稽核」變成雙方共識而非監控

---

## 驗證點（per wave）

**Wave 1**
- RAG 回覆可看到 citations，點 `[1]` 能開抽屜
- 輸入身分證號碼會被提示並在送出後顯示遮罩 span
- 每則 assistant message 底下能複製 trace_id

**Wave 2**
- 低信心回覆會自動顯示 follow-up 建議
- Router handoff 時對話流顯示節點 timeline
- 比較模式可同時送同一問題給兩個 agent 並排顯示

**Wave 3**
- 對話可切換到機密模式，複製 / 匯出功能立即失效
- 可將對話歸入「HR」資料夾並以 `tag:hr` 搜尋
- 分享連結可設定過期時間並被同組織同事開啟

**Wave 4**
- 輸入 `@code-assist` 可直接繞過 router
- 多個 @ mention 會觸發 parallel dispatch

**Wave 5（Onyx 就緒後）**
- 「我要請下週三一天假」可完成從 collect → confirm → submit → done 的完整流程
- 送出前必顯示確認卡片；確認後產生稽核可追蹤的 job_id
