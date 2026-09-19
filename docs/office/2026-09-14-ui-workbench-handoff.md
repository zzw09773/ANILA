# HANDOFF — ANILA L1 全站介面盤點全改 + 企業工作台視覺

- 角色：ANILA_GrokWriter
- 日期：2026-09-14
- Worktree：/tmp/anila-ui-workbench-20260914
- Branch：codex/ui-workbench-20260914
- 基底 / HEAD：84cdfcf9（未 commit）
- main checkout 未動；未 merge / push / tag / deploy

## 改了哪些檔

共用：PageHead.vue、PageState.vue、RowActions.vue、tokens.css、main.css、statusLabel.js、settingsView.js、alertSummary.js
治理頁：Dashboard、Models、Settings、Users、Alerts、ApiKeys、Knowledge、CollectionDetail、Departments、DeveloperGuide、Feedback、ServiceAccess、ServiceClients、ModelAccessGroups、Banners、AuditLogs、Usage、AlertSummaryCard
對話端：shellNav.jsx、app.jsx、chat.jsx、components.jsx、RouterModelPicker.jsx、trust.jsx 與對應測試

## 盤點對應

1. 已做 — shellNav 使用者可見名稱「對話」，id 仍 tasks。
2. 已做 — Users 勾選 aria-label；關聯三下拉有標籤；彈窗關閉有名稱。
3. 已做 — 已確認未解決警報 tone=warn；標籤「未處理高嚴重度」。後端口徑未改。
4. 已做 — 開發指南正式走 CA；SSL_VERIFY=0 標開發捷徑；system.md 路徑統一。
5. 已做 — Router 群組／公告／服務客戶端頁首與空狀態對齊。
6. 已做 — 儀表板「健康模型」「近 24h 有呼叫的金鑰」。未改計算。
7. 已做（前端）— bytes_stored 為 0/空顯示 —。後端彙總未修。
8. 已做 — 使用者列常用外露、其餘更多；僅 SSO 與刪除模型登錄正名。
9. 已做 — 角色篩選補擁有者／系統。
10. 已做 — 平台設定三組、布林開關、單位、逐項生效時機。
11. 已做 — 隱私文案 warn/block 中文；金鑰權杖密碼只提醒。
12. 已做 — 回饋寫在對話端開啟；服務存取連到平台連結。
13. 部分 — 儀表板舊憑證文案中文化；知識庫副標去掉 RAG env。
14. 已做 — 警報／稽核／知識庫狀態中文；部門直屬／含下層。
15. 已做 — 告警摘要與金鑰列表分開載入中／空狀態。

## 測試

- node --test tests/alertSummary.test.mjs tests/settingsOverview.test.mjs PASS
- csp-governance-ui npm run build PASS
- vitest shellNav + reasoningTimeline + routerModelPicker + settingsPrivacyHonesty PASS
- node_modules 用 main checkout symlink，跑完已拆，未改 main package.json

## 未做

- 知識庫 bytes_stored 後端彙總（ingestion/CSP）
- headed 320px／鍵盤走查／對比／E2E
- 模型列尚未收進 RowActions（名稱已改）
- 未 commit

## 風險

- 設定分組依 key 前綴，新 C 類 key 可能落到模型與檢索
- 布林草稿以字串 true/false 送回
- 使用者「更多」用 details，未做點擊外部關閉

下一棒：指揮官驗磁碟。不要送 Astra，不要合 main。

## 本輪補做（視覺工作台，2026-09-14 第二趟）

真正套用 PageHead 的頁面：

- 儀表板（樣板；警報在用量之前）
- 模型（樣板；列操作收進 RowActions）
- 平台設定（樣板；載入／失敗用 PageState）
- Router 授權群組
- 公告橫幅
- 服務客戶端
- 助手（原 Agent 頁）
- 自訂動作
- 平台連結
- 信任主機

TermBox 拿掉深色標題帶。圖表 Y 軸標「請求」、X 軸不再傾斜。
對話端：助手／模型分層標籤；輸入框下方靜態 Enter 提示拿掉；設定改為上方頁籤＋直向欄位。
測試：alertSummary、settingsOverview、governance build、shellNav、reasoningTimeline、routerModelPicker、privacy 皆 PASS。
仍未 commit。下一棒指揮官驗磁碟。

## 人員目錄改名（L0）

- 側欄「主要」拿掉群組。admin 新增「人員與單位」：使用者、部門、群組。非 admin 看不到群組。
- 群組頁 PageHead 改「群組」，並連到模型頁「可使用對象」。
- 模型表單「可使用對象／個人／新增對象」。checkbox 仍是「開放給對話模型選單」。
- 刊頭 PAGE_LABELS 補群組。路徑與 API model-access-groups 未改。
