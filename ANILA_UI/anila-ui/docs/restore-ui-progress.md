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
   - `/v1/chat/completions`（streaming + JSON 兩條路徑 + agent / model 兩種 resolve）：從 resolved agent/model 取 `requires_encryption`（agent 會 fallback 看 `base_model.requires_encryption`）帶給 `proxy_request` / `proxy_stream`，並在非串流 agent 直連時也 `setdefault`/升級 `existing_meta["classified"] = True`
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
- **Router 需讓 CSP 先標 classified**：目前 plan 是由 CSP 在 `anila_meta.classified` 直接標示；router 只要 propagate，就不需要重複查 `model_registry`
- **UI 不會把 classified 降級**：即使某一輪 `meta.classified=false`，UI 也不下拉，因為一條對話只要曾用到機敏模型就整條鎖住（符合資安直覺）
- **API Key 僅存在 `sessionStorage`**；JWT 存 `localStorage`（與原先版本一致）
- **OIDC 流程**：由瀏覽器導到 `/api/auth/oidc/:provider/start`，回流到 `/app` 由後端 session 建立；若後端尚未支援自動回帶 API Key，需要使用者登入後再手動在設定中補上

