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

### A. 前端頁面 (優先) — ✅ 完成 (2026-04-21)
1. **`src/chat.jsx`** ✅ — 依 template 完整 port 成 ESM：
   - `TraceRow`, `RoutingTrace`（讀 `window.ANILA_TWEAKS.traceStyle`）
   - `MessageBubble`（含 citations / confidence / handoff timeline / follow-ups / redaction / classified corner）
   - `AgentSelector`（trigger + MenuItems 都含 `requiresEncryption` lock badge）
   - `Composer`（PII hints + `@mention` 高亮、附件、`Shift+Enter` 換行、Enter 送出；支援 `placeholder` + `footer` props）
   - `Sidebar`（資料夾 chips、tag 搜尋、Agents tab、使用者選單；相容 `c.agent`/`c.agentId`、`c.ts`/`c.updatedLabel`）
   - **不保留任何 user-controlled「鎖定對話」IconButton / 切換動作**

2. **`src/app.jsx`** ✅ — `ChatRuntime` 接到真實後端：
   - `refreshAgents()` 從 `GET /v1/agents` 取資料 → `normalizeAgents` 把 `requires_encryption` → `requiresEncryption`；前置 `ROUTER_AGENT`
   - `sendMessage()` 依 target 決定 baseUrl：`VITE_ROUTER_BASE_URL` (target === `"anila-router"`) vs `VITE_CSP_BASE_URL`；透過 `streamChatCompletion` 串 `onText` / `onTrace` / `onMeta`
   - `ensureConversation()`、`updateConversationAgent()`、`applyMeta()` 三點 latch：`c.classified || agent.requiresEncryption || meta.classified`（**一旦鎖上永不降級**）
   - 頂欄沒有任何 lock/unlock IconButton；「加密模式」chip 唯讀；`classified` 時 share/copy disabled
   - `ParallelCompareView` 把 `AgentSelector`, `Composer`, `MessageBubble` 以 props 注入（避免循環 import）
   - `SettingsModal`（general/apikey/privacy/account/about）、`TweaksPanel`、`ShareDialog`、`ApiKeyPopover` 都已接好
   - `App` 預設輸出掛載 `applyTweaks` + `window.ANILA_TWEAKS` + 父視窗 `postMessage` edit-mode

3. **`src/main.jsx`** ✅ — `BrowserRouter` + `AuthProvider` + `RequireAuth` 守門 + Routes：
   - `/login` → `<RedirectIfAuthed><LoginView/></RedirectIfAuthed>`
   - `/app/*` → `<RequireAuth><App/></RequireAuth>`
   - catch-all → `<Navigate to="/app" replace/>`
   - `!authReady` 時顯示 `.boot-screen`

4. **Smoke check** ✅
   - `npm install`：160 packages
   - `npm run build`：43 modules transformed，dist/assets/index-*.js 258.59 kB gzip 79.46 kB

### B. 後端：強制加密規則由伺服器側決定 (close-loop) — ✅ 完成 (2026-04-21)
1. **CSP (`myCSPPlatform/backend/app/services/proxy_service.py`)** ✅
   - `build_default_anila_meta(...)` 接收 `classified: bool = False` 參數；`proxy_request`、`proxy_stream` 新增 `requires_encryption: bool = False` 參數
   - `proxy_stream` 內部 `_emit(block, event_name, data)` 會解析下游 `anila.meta` 並在 `requires_encryption=True` 時把 `classified` 補為 `True`（一律單向 latch）
2. **CSP (`myCSPPlatform/backend/app/api/proxy.py`)** ✅
   - `/v1/chat/completions` agent 路徑（streaming + JSON）：從 resolved **agent** 直接讀 `requires_encryption`（base LLM 不再帶旗標）帶給 `proxy_request` / `proxy_stream`，並在非串流 agent 直連時也 `setdefault`/升級 `existing_meta["classified"] = True`
   - `/v1/chat/completions` 直連 LLM 路徑：一律以 `requires_encryption=False` 呼叫 proxy；classified 只靠下游自己 meta 帶上來（agent 才是 classification 的來源）
3. **Router (`AgenticRAG/src/anila_core/api/router_server.py` + `registry/remote_agent_manifest.py`)** ✅
   - `RemoteAgentManifest` 新增 `requires_encryption: bool`，從 `/v1/agents` response 的 `requires_encryption` 欄位讀取
   - `_merge_anila_meta(..., classified_override: bool = False)`：`classified_override or merged.get("classified")` 任一為真就把 `merged["classified"] = True`（單向 latch）
   - dispatch-to-agent 分支傳 `classified_override=bool(manifest.requires_encryption)`；direct LLM 分支透過 `_normalize_anila_meta` 讓下游 meta 的 `classified` 自然保留

### C. 交付
- 各階段分別 commit（WIP → feat → backend hooks → build verification）
- `git push -u origin claude/restore-ui-from-template-VWf99`
- 不主動開 PR（除非使用者另外指示）

---

## 風險 / 注意事項
- **Router 需讓 CSP 先標 classified** ✅ 已完成（2026-04-21）：由 CSP 在 `anila_meta.classified` 直接標示；router 只要 propagate，不需要重複查 `model_registry`
- **UI 不會把 classified 降級** ✅ 已完成（2026-04-21）：`applyMeta` / `ensureConversation` / `updateConversationAgent` 三處全為單向 latch；一條對話只要曾用到機敏模型就整條鎖住
- **API Key 僅存在 `sessionStorage`**；JWT 存 `localStorage`（與原先版本一致）
- **OIDC 流程** ✅ 已完成（2026-04-21，Wave A）：`myCSPPlatform/backend/app/api/auth.py` 的 `oidc_callback` 現會呼叫 `_mint_sso_api_key` 產生綁定所有 active model、24h 有效的 short-lived API Key，並在 callback HTML 內以 `sessionStorage.setItem('anilaRuntimeApiKey', ...)` 自動寫入前端。沒有 active model 時會 `return None`，SPA 降級回 API-Key popover。

---

## 後續 Wave（`anila_plan.md` 對齊）

- **Wave A** ✅ 已完成（2026-04-21）：`/api/conversations`、`/api/attachments`、`/api/conversations/{id}/shares`、`/api/handoffs` 全數接線；`runtime/conversations.js` 集中所有 CSP control-plane wrapper；OIDC API Key auto-bind 完成。
- **Wave B** ✅ 已完成（2026-04-21）：Router 真 SSE streaming + 錯誤降級 + registry visibility
  - `AgenticRAG/src/anila_core/api/router_server.py`
    - 新增 `_stream_agent_sse` async generator：`stream=True` 且有 dispatch 時直接 forward agent SSE chunks（不再等整份結果 replay）
    - 新增 `_dispatch_safe` + `_call_llm_non_stream` 全面加上 try/except：upstream 5xx/timeout/connection error 降級成 trace step + 使用者友善訊息，**不再 500**
    - `/health` 回報 `last_refresh_error` + `last_refresh_at`，registry refresh 失敗不再靜默
  - `AgenticRAG/src/anila_core/registry/remote_agent_manifest.py`：新增 `last_refresh_error` / `last_refresh_at` property，`_do_refresh` 在成功/失敗時同步更新
- **Wave C** ✅ 已完成（2026-04-21）：Root `README.md` 新增，收錄 compose 一鍵啟動、本地 LLM endpoint 對接（Ollama / vLLM / llama.cpp）、環境變數對照表、Router `/health` 判讀。`docker-compose.yml` 已對齊 on-prem（無 mock LLM）。
- **Wave D** ✅ 已完成（2026-04-21）：classified latch 測試覆蓋 + UI pure helper 抽離。
  - CSP pytest (`myCSPPlatform/backend/tests/test_proxy_classified.py`)：12 tests — `build_default_anila_meta` 行為 + 非串流 latch 組合真值表
  - Router pytest (`AgenticRAG/tests/test_router_classified.py`)：16 tests — `_merge_anila_meta` 單向 latch、`RemoteAgentManifest.requires_encryption` 流動、handoff_chain shape、classified_override × downstream 組合
  - UI vitest (`ANILA_UI/anila-ui/src/__tests__/`)：30 tests — `sse.test.js` (9)、`classified.test.js` (14)、`normalizeAgents.test.js` (7)
  - 新增 `ANILA_UI/anila-ui/src/runtime/classified.js` pure helper (`computeConversationClassified` / `appendClassifiedTag` / `latchConversationWithMeta`)；`app.jsx` 的 `applyMeta` 改用此 helper，確保 UI latch 邏輯可被測試覆蓋
- **Wave E / F**：依 `anila_plan.md` 排程延後（Phase 4 視覺重設計 + `build_app(mode="router")` factory + P2/P3）。

