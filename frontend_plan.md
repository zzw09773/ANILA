# 全前端完成前的現況分類與收尾計畫

## Summary

以你剛剛鎖定的範圍為準：

- 範圍是 **全前端**：`ANILA_UI/anila-ui` runtime client + `myCSPPlatform/frontend` 控制面
- 優先順序是 **Runtime 先收斂，再補控制面**
- 「前端完成」定義為 **產品層完成**：畫面、互動、狀態、空狀態、錯誤態都要完整；尚無後端契約的功能可先用 local/mock
- **不包含** `trust-collab-workflow-plan.md` 裡的 Onyx schema-driven workflow UI；那一塊留到下一階段

## 現況分類

### 1. 已完成
- Runtime 的設計系統已經從 `ANILA_templete` 落到 active UI，主視覺、login、sidebar、chat、compare、citation drawer 已不再是 prototype 等級。
- Runtime 已接真實 `/v1/agents` 與 `/v1/chat/completions`，SSE 串流、citation、trace、confidence 顯示都已接上現有 Router/CSP metadata。
- `@agent`、PII redaction、compare mode、採用比較結果、folder/tag/starred 搜尋這些互動已存在。
- Attachment 已有前端 metadata flow：可附檔、顯示 chip、進 compare/share 文本。
- 控制面前端不是空的：developer route、developer role、Agent 管理頁、Users 可用 Agent 指派、API keys / usage / audit / models / users 這些畫面都已存在並接 API。

### 2. 只是假 UI / 前端殼
- Runtime 對話、folder、tag、starred、classified、handoff、share 目前本質上是 **前端記憶體狀態**；重新整理後不保留，也沒有 server-side conversation model。
- `classified` 目前只是前端切換與按鈕禁用；還沒有「不可逆、只能 admin 解鎖、每次互動寫 audit」的真約束。
- `share` 目前只是複製摘要文字，不是分享連結、沒有唯讀頁、沒有 expiry / fork / view log。
- `handoff` 目前只會插一則本地系統訊息並切 target；沒有交接給同事、沒有通知、沒有 ownership/pending/accepted 狀態。
- `attachment` 目前只是檔名/大小 metadata；沒有 upload、preview URL、server reference、也不會真的進模型上下文。
- `@agent` 現在是文字 parser，不是 template/規格中的 tokenized mention chip。
- compare 還缺產品化細節：row-level diff/highlight、退出比較時的合併策略選擇、欄位狀態更完整的空/錯誤態。
- audit UX 只做了一部分：現在有 trace/citation 呈現，但還沒有完整 `trace_id + conversation_id + latency` watermark、copy meta、report issue 入口、隱私設定開關。
- 控制面雖已接 API，但仍偏「可操作管理頁」，不是「產品完成」：缺搜尋/篩選/批次動作/狀態反饋/詳情資訊密度/空與錯誤態一致性。

### 3. 真的要補後端
- Conversation persistence、share link、readonly shared page、fork、share permission、share expiry、view log。
- Classified 的 authoritative policy：不可逆解鎖、audit log、server-side classification、copy/export enforcement。
- Attachment 真上傳：檔案儲存、引用 ID、payload schema、agent/router 可讀取附件。
- Handoff 給 user：通知、接受/拒絕、ownership、可見性、歷史紀錄。
- 真正的 audit watermark 所需欄位保證：`conversation_id`、`trace_id`、`latency_ms`、view/reply log。
- Workflow module 若未來要做，必須有結構化 workflow schema 與 job/polling 契約。

## 前端完成計畫

### A. Runtime 先做到產品完成
- 把 runtime conversation state 從純 React state 升級為 **browser-persisted client state**。
  預設：用 `localStorage`/`sessionStorage`，不等待後端 conversation API。
- 補齊 trust/collab UX，使其即使先走 local/mock 也像完整產品：
  - `classified`：加入送出前確認、右下浮水印、側欄鎖圖示、一旦上鎖在前端不可直接降級。
  - `share`：做完整分享面板，包含 expiry / readonly / fork / recipient 選項；若後端未啟用，生成本地 share draft 與唯讀預覽頁，不假裝是正式 server link。
  - `handoff`：支援 `agent / user` 兩種交接 tab；對 user handoff 先做本地通知/inbox 狀態模型。
  - `audit watermark`：每則 assistant message 顯示 `trace / conv / timestamp / latency`，支援一鍵複製完整 meta。
- 把 mention / compare / attachment 收到 template-level 完成度：
  - `@agent` 改成 token chip UX，而不是只靠文字 parser。
  - compare 增加 hover 對齊、欄位空/錯誤/loading 狀態、退出比較時的 adopt/merge/cancel 選擇。
  - attachment 增加 image/file preview card、限制提示、local object URL 預覽；仍不做真 upload。
- 把 sidebar/history 產品化：
  - folder/tag/starred 持久化。
  - 搜尋語法收斂為 `tag:` 為主，不新增更多查詢語法。
  - conversation card 顯示 classified/starred/target/updated/meta 的穩定資訊。
- 新增清楚的「未同步」語意。
  預設：凡是尚無後端契約的 share/handoff/attachment capability，都在 UI 上標示 `local draft` 或等價文案，避免誤導成真同步功能。

### B. 控制面前端補到產品完成
- `DeveloperAgentsView` 由可操作管理頁補成完整 console：
  - search/filter/sort
  - approval queue 視角
  - richer detail drawer：capabilities、base model、owner、status history、download result feedback
  - register form 驗證與更完整錯誤提示
- `UsersView` 補成完整權限編排頁：
  - 角色、allowed models、allowed agents 的視覺整合
  - 批次操作與更穩定的成功/失敗反饋
  - approved / active / developer 狀態切換的 guardrail 文案
- 補齊控制面共同體驗：
  - toast/confirm/loading/error/empty state 一致化
  - route guard 與 role badge 文案收斂
  - 重要頁的 mobile/窄寬度可用性
- 不新增新控制面模組。
  預設：沿用現有 Vue routes/views，只做產品化、信息架構和互動補強。

### C. 前端邊界與介面
- 這一輪 **不新增後端 API**；前端完成以現有 API + client-only state 為邊界。
- Runtime 內部資料模型需明確擴充為可持久化的 client schema：
  - `conversation`: `id/title/agentId/agentName/folder/tags/starred/classified/shareDraft/handoffState/updatedAt`
  - `message`: `id/role/text/attachments/piiHits/explicitAgents/trace/citations/confidence/traceId/conversationId/latencyMs/handoffChain`
  - `shareDraft`: `mode/expiry/allowFork/recipientScope/recipients`
  - `handoffState`: `type(agent|user)/target/status/note/requestedAt`
- 控制面不改 API shape；只在前端增加 view-model、filter state、validation state。

## Test Plan

- Runtime：
  - `npm run build`
  - `npm run test -- --run`
  - 手測：login、API key gate、agent load、單聊、compare、citation drawer、classified、share draft、handoff draft、attachments、reload 後持久化
- 控制面：
  - `npm run build`
  - 手測：admin / developer / user 三角色路由與 sidebar
  - 手測：DeveloperAgentsView、UsersView、allowed agents/models、template download、error/loading/empty states
- 視覺驗收：
  - 以 `ANILA_templete` 為基線，不再追 PDF pixel-perfect；PDF 只當最終氣質檢查
  - Desktop + mobile 兩個斷點都要驗

## Assumptions

- `ANILA_templete` 繼續作為 runtime UI 的 canonical design source；不把 template 本身產品化上線。
- Workflow UI 不進這一輪。
- 這一輪允許 local/mock，但凡無後端支援的功能都必須明示為未同步狀態。
- 控制面現有 API 已足夠支撐前端產品化；若實作中發現 API 缺欄位，再另開後端契約清單，不在這一輪前端計畫內直接決策。
