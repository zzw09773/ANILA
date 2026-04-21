# ANILA UI Restore — Progress & Remaining Plan

**Branch**: `claude/restore-ui-from-template-VWf99`
**目標**:
1. 依 `ANILA_UI/ANILA_templete/anila-ui/` 的設計，把 `anila-ui/` 重建為「近乎一致」的前端
2. 所有資料與呼叫都串到真正的後端（CSP + Router），不再使用 mock
3. 加密模式改為**後端驅動**：一旦用到需加密的模型（agent `requires_encryption` 或 meta 回報 `classified`），對話自動上鎖，**USER 不得透過 UI 切換**

---

## 當前進度 (已完成)

### 1. 備份與清理
- `anila-ui/` 完整內容備份至 `ANILA_UI/scraps/anila-ui-backup-2026-04-21/`（含先前的 `runtime/` 版本）
- 原本 `src/` 下的舊檔案全部清掉（`app.jsx`, `chat.jsx`, `main.jsx`, `runtime/app-page.jsx`, `runtime/login-page.jsx`, `styles.css` …）

### 2. 基礎層 (ESM 化)
- `index.html` — 對齊 template 的 CSS 變數、字型、`ANILA_TWEAKS`、`@keyframes anila-blink`、`.boot-screen`；entry point 改為 `/src/main.jsx`
- `src/icons.jsx` — 約 45 個 `Icon*` 元件 + `AnilaGlyph` （ESM `export const`）
- `src/components.jsx` — `Button`, `IconButton`, `AgentPill`, `Kbd`, `Divider`, `Dropdown`, `MenuItem`, `Modal`, `Input`（ESM）

### 3. 功能模組 (ESM 化，忠實 port 自 template)
- `src/trust.jsx` — `CitationInline`, `renderTextWithCitations`, `CitationsDrawer`, `RedactionHint`, `RedactedSpan`, `RenderRedactedText`, `ConfidenceChip`, `FollowUpSuggestions`, `AuditWatermark`, `ClassifiedCorner`, `ConfidentialWatermark`
  - 已加上對真實後端 citations 的欄位 guard（`score` / `section` / `snippet` / `source_uri` / `updated_at` 為可選）
- `src/data.jsx` — 僅保留 `FOLDERS`（含 `compared` 分類）、`detectPII`, `maskPII`, `renderWithRedaction`（**移除所有 mock agents / conversations / fake response**）
- `src/multiagent.jsx` — `HandoffTimeline`, `ParallelCompareView`, `parseMentions`, `HighlightedMentions`
  - `ParallelCompareView` 接收 `AgentSelector`/`Composer`/`MessageBubble` 作為 props，避免與 `chat.jsx` 形成循環 import
- `src/collab.jsx` — `ShareDialog`, `HandoffMenu`, `TagEditor`
- `src/tweaks.jsx` — `TweaksPanel`（`window.parent.postMessage` 加上 try/catch 防護）

### 4. Runtime / Backend 接線層
- `src/runtime/api.js` — `config`（`VITE_CSP_BASE_URL` / `VITE_ROUTER_BASE_URL`），`authRequest`, `refreshJwt`, `authRequestWithRefresh`, `apiKeyRequest`
- `src/runtime/auth.jsx` — `AuthProvider`, `useAuth`, `useLogoutRedirect`
  - 啟動時抓 `/api/auth/providers`、如有 JWT 再打 `/api/auth/me`
  - `validateApiKey()`：呼叫 `GET /v1/agents` 驗證 key；通過才存到 `sessionStorage`
  - `login({username, password, authSource, providerId, apiKey})`：支援 `local` / `ldap` / `oidc`（OIDC 由前端另外導流）
- `src/runtime/sse.js` — `parseSseBlocks`, `parseSseEvent`, `streamChatCompletion({url, apiKey, payload, onText, onTrace, onMeta, onJson})`；會分開 dispatch `anila.trace` / `anila.meta` 事件

### 5. 頁面層（已完成的部分）
- `src/login.jsx` — 兩欄版面（品牌面板 + 表單），method tabs（本機 / LDAP / SSO），透過 `useAuth().login(...)` 或 `/api/auth/oidc/:provider/start` 真正登入；API Key 合法性於 `useAuth.validateApiKey` 內驗證

---

## 剩下要做 (Remaining Plan)

### A. 前端頁面 (優先)
1. **`src/chat.jsx`** — 依 template 完整 port 成 ESM：
   - `TraceRow`, `RoutingTrace`（讀 `window.ANILA_TWEAKS.traceStyle`）
   - `MessageBubble`（含 citations / confidence / handoff timeline / follow-ups / redaction / classified corner）
   - `AgentSelector`, `Composer`（PII hints + `@mention` 高亮、附件、`Shift+Enter` 換行、Enter 送出）
   - `Sidebar`（資料夾 chips、tag 搜尋、Agents tab、使用者選單）
   - **不保留任何 user-controlled「鎖定對話」IconButton / 切換動作**

2. **`src/app.jsx`** — `ChatRuntime` 接到真實後端：
   - `refreshAgents()` 從 `/v1/agents` 取資料 → `normalizeAgents` 把 `requires_encryption` → `requiresEncryption`；前置 `ROUTER_AGENT`
   - `sendMessage()`：
     - 路由到 `VITE_ROUTER_BASE_URL + /v1/chat/completions`（當 agent 為 `anila-router`）或 `VITE_CSP_BASE_URL + /v1/chat/completions`（direct target）
     - 透過 `streamChatCompletion` 串 `onText` / `onTrace` / `onMeta`
   - `ensureConversation()`：新對話建立時把 `classified: agent.requiresEncryption` 直接寫入 conversation；**不允許使用者解除**
   - `applyMeta()`：每次收到 `anila.meta` 時，如果 `meta.classified === true` 則把 conversation 狀態升為 classified（**一旦鎖上就不降級**）
   - `updateConversationAgent()`：切換 agent 時若新 agent `requiresEncryption` 亦自動上鎖
   - 頂欄刪掉 template 的「設為機密 / 解除機密」`IconButton`；保留「加密模式」唯讀標示與 classified 情況下「分享/複製」的 disabled 行為
   - `ParallelCompareView` 把 `AgentSelector`, `Composer`, `MessageBubble` 用 props 注入
   - `adoptCompareColumn()` / compare-merge 流程照 backup 版本搬過來
   - `SettingsModal`, `TweaksPanel`, `ShareDialog`, `ApiKeyPopover` 一併掛上

3. **`src/main.jsx`** — `BrowserRouter` + `AuthProvider` + `RequireAuth` 守門 + Routes：
   - `/` → Navigate 到 `/app` 或 `/login`
   - `/login` → `<LoginView/>`
   - `/app` → `<RequireAuth><ChatRuntime/></RequireAuth>`
   - `!authReady` 時顯示 `.boot-screen`

4. **Smoke check**
   - `npm install` → `npm run build`：確認 Vite 可打包、沒有 import 循環
   - `npm run dev`：瀏覽器打開確認基本渲染

### B. 後端：強制加密規則由伺服器側決定 (close-loop)
1. **CSP (`myCSPPlatform/backend/app/services/proxy_service.py`)**
   - 擴充 `build_default_anila_meta(...)` 接收 `classified: bool` 參數
2. **CSP (`myCSPPlatform/backend/app/api/proxy.py`)**
   - `/v1/chat/completions`（streaming + JSON）：解析出 resolved model / agent 後，把該 model 或 agent 的 `requires_encryption` 帶進 `anila_meta.classified`
   - Streaming：在 `event: anila.meta` payload 中夾帶 `classified`
3. **Router (`AgenticRAG/src/anila_core/api/router_server.py`)**
   - `_merge_anila_meta` / `_default_anila_meta`：若下游 meta 的 `classified === true` 或被分派 agent 本身 `requires_encryption`，把 router meta `classified` 也設為 `true`
   - 確保 `/v1/chat/completions` 向前轉發時不會把這個欄位漏掉

### C. 交付
- 各階段分別 commit（WIP → feat → backend hooks → build verification）
- `git push -u origin claude/restore-ui-from-template-VWf99`
- 不主動開 PR（除非使用者另外指示）

---

## 風險 / 注意事項
- **Router 需讓 CSP 先標 classified**：目前 plan 是由 CSP 在 `anila_meta.classified` 直接標示；router 只要 propagate，就不需要重複查 `model_registry`
- **UI 不會把 classified 降級**：即使某一輪 `meta.classified=false`，UI 也不下拉，因為一條對話只要曾用到機敏模型就整條鎖住（符合資安直覺）
- **API Key 僅存在 `sessionStorage`**；JWT 存 `localStorage`（與原先版本一致）
- **OIDC 流程**：由瀏覽器導到 `/api/auth/oidc/:provider/start`，回流到 `/app` 由後端 session 建立；若後端尚未支援自動回帶 API Key，需要使用者登入後再手動在設定中補上

