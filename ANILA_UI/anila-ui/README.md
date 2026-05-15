# ANILA Runtime UI

ANILA 平台的前端 Runtime（React + Vite），讓終端使用者登入後與 agent 對話、分享對話、交接、上傳附件、使用 `anila-router` pseudo-agent 讓主 LLM 自動分派。

> 關於平台整體架構、compose 啟動、環境變數參照請看 repo 根 [`README.md`](../../README.md) 與 [`anila_plan.md`](../../anila_plan.md)。本 README 聚焦 UI 子專案本身。

---

## 對接對象

| 對象 | 路徑 | 認證 |
|---|---|---|
| **CSP Control Plane** | `/api/*` | JWT（自動 refresh） |
| **CSP Data Plane** | `/v1/*` | CSP API Key（`sk-...`） |
| **ANILA Router** | `/v1/*` | CSP API Key（pseudo-agent `anila-router`） |

---

## UI 運作流程

```mermaid
flowchart TB
    enter["🧑 使用者進入 UI"]
    auth{"已登入?"}
    login["/login<br/>本機帳密 or OIDC"]
    main["ChatRuntime 主介面<br/>GET /api/conversations<br/>GET /v1/agents"]
    pick["選對話 / 新對話 / 選 agent"]
    send["Composer 送訊息<br/>POST /v1/chat/completions (SSE)"]
    render["逐 chunk render<br/>message_delta / tool_call / meta"]
    classified{"SSE meta<br/>classified=true?"}
    latch["對話 one-way latch<br/>升級為 classified<br/>(UI 無降級路徑)"]
    done["收到 [DONE]"]
    persist["POST /api/conversations/{id}/messages<br/>(user + assistant 寫回 CSP)"]
    collab["分享 / 交接 / 附件<br/>POST /shares · /handoffs<br/>multipart /attachments"]

    enter --> auth
    auth -- no --> login
    login --> main
    auth -- yes --> main
    main --> pick
    pick --> send
    pick -.->|並行| collab
    send --> render
    render --> classified
    classified -- yes --> latch
    latch --> done
    classified -- no --> done
    done --> persist

    classDef secure fill:#fee2e2,stroke:#dc2626
    class latch secure
```

<details>
<summary>📄 ASCII 版本</summary>

```
┌─────────────────────────────────────────────────────────────┐
│                      使用者進入 UI                            │
└──────────────────────────┬──────────────────────────────────┘
                           │ 未登入？
                           ▼
            ┌──────────────────────────────┐
            │  /login（帳密 or OIDC）       │
            │  - 本機帳號 → 後端 /api/auth/ │
            │    login 取 JWT              │
            │  - OIDC → 後端 callback 直接  │
            │    回傳 JWT + 24h API Key     │
            └──────────────┬───────────────┘
                           │ JWT 放 localStorage
                           │ API Key 放 sessionStorage
                           ▼
            ┌──────────────────────────────┐
            │     主介面 ChatRuntime        │
            │  - GET /api/conversations    │
            │    載入對話列表               │
            │  - GET /v1/agents             │
            │    載入 agent manifest        │
            │    （含 requires_encryption） │
            └──────────────┬───────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │  使用者選對話 / 按「新對話」／選 agent               │
    └────────────┬───────────────────────────────┬─────────┘
                 │                               │
                 ▼                               ▼
      ┌─────────────────────┐         ┌─────────────────────┐
      │ Composer 送訊息      │         │ 附件 / 分享 / 交接   │
      │ POST /v1/chat/       │         │ 見下方區塊           │
      │ completions (SSE)    │         └─────────────────────┘
      │   ↓                  │
      │ 逐 chunk render       │
      │   ↓                  │
      │ SSE meta classified? │──Yes──▶ 整個對話 one-way
      │                      │         latch 為 classified
      │   ↓                  │         （UI 不能降級）
      │ 收到 [DONE]           │
      │   ↓                  │
      │ POST /api/            │
      │ conversations/{id}/   │
      │ messages              │ 把 user / assistant 訊息
      │ (persist)             │ 寫回 CSP
      └──────────────────────┘
```

</details>

### 分享 / 交接 / 附件

```
分享：      ShareDialog → POST /api/conversations/{id}/shares
            → 回傳 /s/c/<token> 連結

交接：      HandoffMenu → POST /api/handoffs
            → 對方於 /handoffs 頁接受（accept）/ 拒絕（reject）

附件：      Composer onAttach → POST /api/attachments (multipart)
            → 回傳 attachment_id → 一併送進 /v1/chat/completions
```

<!-- Wave 2 / Sprint 7 X 相關過時內容已於 Sprint 8 X / Phase H 從本檔
     全面同步，不再需要警語提醒。如要追溯舊版寫法，看 git log。 -->

---

## 先決條件

- Node **20+**（Dockerfile 固定 22）
- 可連線的 CSP（`myCSPPlatform` `:8000`）
- 可連線的 Router（`anila-core-router` `:9000`）
- 一個落地 LLM endpoint（供 CSP 轉發）

---

## 安裝與啟動

```bash
cd ANILA_UI/anila-ui
cp .env.example .env.local
# 編輯 .env.local（若 CSP / Router 不在 localhost）
npm install
npm run dev
```

打開 <http://localhost:5173>，首次進入會重導到 `/login`：

1. 用 CSP 帳號登入（本機帳密或 OIDC）。SPA 完全走 cookie session（Wave 2 / Sprint 7 X 後不再需要、也不再支援前端輸入 API Key）。
2. 登入完即可直接開始對話 — 所有 `/api/*` + `/v1/*` 請求都用 httpOnly cookie 認證。
3. SDK / curl 路徑仍可走 `Authorization: Bearer sk-…`；但這條 path 不在 SPA 的職責內，要用 SDK 請至 CSP `/api-keys` 自行 provision。

## 環境變數

| 變數 | 用途 | 預設 |
|---|---|---|
| `VITE_CSP_BASE_URL` | Control Plane（JWT `/api/*`）+ Data Plane（`/v1/*`）基底 | `http://localhost:8000` |
| `VITE_ROUTER_BASE_URL` | ANILA Router 基底（`anila-router` pseudo-agent） | `http://localhost:9000` |

若其中任一未設，`src/runtime/api.js` 會在 boot 時 `console.warn` 標示缺失。空值會 fallback 成相對路徑，只有在反向代理同時 front 這兩個服務時才能正常運作。

## Scripts

| 指令 | 作用 |
|---|---|
| `npm run dev` | Vite dev server（HMR）`:5173` |
| `npm run build` | Production build 到 `dist/` |
| `npm run preview` | 在本機跑 production build |
| `npm test` | Vitest 測試（目前範圍逐步擴大中 — 見 `anila_plan.md` Wave D） |

---

## 專案結構

```
src/
├── app.jsx              # ChatRuntime — agent 選擇、送訊息、persist 對接
├── chat.jsx             # Sidebar、MessageBubble、Composer
├── collab.jsx           # ShareDialog、HandoffMenu、TagEditor
├── trust.jsx            # CitationsDrawer、ConfidentialWatermark
├── multiagent.jsx       # ParallelCompareView（2-3 個 agent 並排比對）
├── tweaks.jsx           # 視覺微調 panel（storyboard）
├── main.jsx             # ReactDOM 掛載入口
├── markdown.jsx         # ReactMarkdown 包裝 + KaTeX / highlight.js 設定
├── components.jsx       # 共用 UI 元件（lock badge、attachment chip 等）
├── agentic.jsx          # Sprint 13 — InterruptCard / TodoChecklist /
│                        # FollowUpChips / PausedBadge
├── toolExecution.jsx    # Sprint 13 — ToolExecutionWidget + Terminal /
│                        # Diff / FileTree / Plain renderer 子元件
├── spanTree.jsx         # Sprint 13 — SpanTreeViewer dev-only viewer
├── icons.jsx            # 自製 SVG icon set
├── data.jsx             # mock / placeholder data 給 EmptyState 等
└── runtime/
    ├── api.js           # fetch wrapper + cookie + CSRF + multipart helper
    │                    # + Sprint 13 getSessionState / submitSessionAnswer
    ├── auth.jsx         # AuthProvider + useAuth hook
    ├── conversations.js # CSP control-plane endpoint wrappers
    ├── sse.js           # SSE parser + dispatchSseEvent (Sprint 13 PR B1)
    │                    # + streamSessionAnswer resume helper
    ├── classified.js    # one-way latch helper（meta → conversation）
    ├── classifyRetryQueue.js  # Sprint 8 X / Phase K — sessionStorage retry
    │                          # queue + page-focus flush 確保 latch 永遠抵達 CSP
    ├── messageMeta.js   # 訊息 metadata 正規化（Sprint 13 加 todos /
    │                    # tool_calls / spans / interrupt 持久化）
    ├── searchSynonyms.js # tag 搜尋同義詞展開（特休 → 年假 / HR）
    ├── time.js          # 相對時間 / ISO 格式 helper
    └── titleClean.js    # LLM 自動產出標題的後處理（去重、placeholder 黑名單）
```

> 註：本 repo 沒有 `src/styles.css` 或 Tailwind import — 全 UI 仰賴 inline 元件樣式 + `index.html` 內含的 base CSS。要全域改 token 請改 `tokens.css`（在主 frontend 或本 SPA 的 index.html）。

---

## 後端端點對應

所有後端 schema 在 `myCSPPlatform/backend/app/api/`：

- `GET/POST /api/conversations` ＋ `GET/PUT/DELETE /api/conversations/{id}`
- `POST /api/conversations/{id}/messages`
- `POST /api/conversations/{id}/shares`（含 `GET` / `DELETE`）
- `POST /api/attachments`（multipart）
- `POST /api/handoffs` ＋ `/accept` `/reject` `/cancel`
- `POST /v1/chat/completions`（SSE）— CSP 或 Router

**Classified 規則由後端決定：** 當 agent `requires_encryption=true` 或 SSE meta 帶 `classified=true`，對話會 one-way latch 成 classified，UI 無降級介面。

---

## Docker

### 單獨 build

```bash
docker build \
  --build-arg VITE_CSP_BASE_URL=http://csp.example:8000 \
  --build-arg VITE_ROUTER_BASE_URL=http://router.example:9000 \
  -t anila-runtime-ui .
docker run -p 8080:80 anila-runtime-ui
```

### 與 CSP、Router 一起跑

**推薦使用 repo 根 [`docker-compose.yml`](../../docker-compose.yml)**，會一起拉起 `csp-db` + `csp` + `redis` + `ingestion-worker` + `router` + `anila-ui` + `anilalm` + `pptx-renderer` + `nginx`：

```bash
cd ../../
docker compose up -d
# UI 經 nginx 對外，預設 https://localhost:4443/ （HTTP 80 / HTTPS 443 也都通）。
# anila-ui container 自身只 expose: 80，沒有 host port mapping —
# 不要去打 localhost:3001（那是舊版部署 doc 的遺跡）。
```

---

## 開發備忘

### 登入流程 (branch SSO 後)

**本 SPA 不自帶登入 UI**。nginx 把 `/login` 強制 redirect 到 myCSPPlatform Vue (`LoginView.vue`,port 443 + 4443 都會跳),所有登入流程 (本機帳密 / OIDC / 中科院憑證卡) 都在那邊處理。本 SPA `runtime/auth.jsx` 只負責:`useAuth()` context 暴露 `user` / `isAuthenticated` / `logout` 給 `ProtectedRoute` 用;`bootstrap()` 跑一次 `GET /api/auth/me` 還原 session (cookie 還在就拿到 user,失效就走 `RedirectToCspLogin` 跳出 SPA)。

```
SPA 載入
  ↓
ProtectedRoute → useAuth().bootstrap()
  ↓
GET /api/auth/me  (cookie: anila_access_token)
  ↓
   ┌─ 200 → user state 填好,進 SPA
   └─ 401 → 試 refresh,失敗則 window.location.assign("/login?next=...")
              ↓
            (nginx redirect 出 SPA → myCSPPlatform Vue LoginView)
              ↓
            登入完跨 port 帶 next 跳回 anila-ui
```

登入後 cookie 三組:`anila_access_token` (httpOnly, SameSite=Lax)、`anila_refresh_token` (httpOnly)、`anila_csrf` (非 httpOnly,JS 讀來放 `X-CSRF-Token` header)。SPA **不再持有** 任何 plaintext token 或 API Key,沒有 localStorage / sessionStorage 寫入路徑。OIDC callback 也不再 mint 24h 短效 API Key (Wave 2 廢除)。

**branch SSO 之後** 本 SPA 內既有的 `login.jsx` / `runtime/card-login.js` / `auth.jsx` 內的 `login()` + `loginWithCard()` 已刪除 (死碼:nginx redirect 後 React route 走不到)。要看實際登入 UI 跟 `caAuth.js` 卡片登入 helper,去 [`myCSPPlatform/frontend/`](../../myCSPPlatform/frontend/)。

### Token refresh

`runtime/api.js` 攔截 401，呼叫 `POST /api/auth/refresh`（cookie 自動帶 refresh token），成功後重送原請求；失敗則踢回 `/login`。整段不經前端 JS — refresh token 全程在 httpOnly cookie 裡。

### SSE parsing

`runtime/sse.js` 把 SSE event header（`event: <name>`）+ data lines 解成 `{ event, data }`，再交給 `dispatchSseEvent`（Sprint 13 PR B1 抽出來、可單獨測）路由到對應 callback：

| event name | callback | 用途 |
|---|---|---|
| `anila.trace` | `onTrace` | 路由 / 分派 trace step（內含 `kind` / `label` / `detail` / `status` / `latency_ms`） |
| `anila.meta` | `onMeta` | 終結性 anila_meta（trace / handoff_chain / citations / follow_ups / classified / latency_ms） |
| `anila.reasoning` | `onReasoning(delta)` | 推理流（reasoning fold） |
| `anila.interrupt_requested` | `onInterrupt` | Sprint 9 ask_user / plan / tool_approval interrupt（觸發 `<InterruptCard>`） |
| `anila.resumed` | `onResumed` | resume 後第一個 chunk 之前送，UI 清掉 paused affordance |
| `anila.todos_updated` | `onTodos` | 任務板全量取代 → `<TodoChecklist>` |
| `anila.follow_ups` | `onFollowUps` | post-turn 建議 → `<FollowUpChips>` |
| `anila.tool_call_started` / `anila.tool_call_finished` | `onToolCallStarted` / `onToolCallFinished` | 工具呼叫狀態 → `<ToolExecutionWidget>` 渲染 |
| `anila.spans` | `onSpans` | OTel 風 span tree → `<SpanTreeViewer>`（dev-only） |
| 未列舉的 `anila.*` | `onUnknownEvent(name, raw)` | forward-compat：新事件不需要前端先升級就能傳到 |
| 預設（無 event header）| `onText` / `onJson` | OpenAI chunk envelope，從 `choices[0].delta.content` 累積文字 |

`X-Anila-Session-Id` response header 透過 `onSessionId(sid)` 回拋給呼叫端，方便 pin 後續 turn 與 resume。

### Resume（Sprint 13 PR B1）

`streamSessionAnswer({ routerBaseUrl, sessionId, interruptId, answer, callbacks })` POST `/v1/sessions/{sid}/answer` 走 Router resume proxy，response 是 SSE stream，dispatch 表跟正常 turn 完全共用。

`getSessionState(sid)` 拉 Router 的 session snapshot，回 `{ messages, pending_interrupts, owner_agent_id }`，UI 可顯示「Resume on <agent>」提示。

### Classified latch 實作

`app.jsx` 的 `applyMeta()`：只在收到 `classified=true` 時把 conversation 升級為 classified，**永不**從 `true` 降回 `false`。這是對應平台規格的 one-way 規則。

Sprint 8 X / Phase K 後，latch 的後端持久化（`POST /api/conversations/:id/classify`）走以下 pipeline：

1. SSE meta 抵達 → React state 立刻 latch（不阻塞 UI）。
2. 若當下 `convId` 已是 numeric server id → 直接打 classify endpoint。失敗進 retry queue。
3. 若 `convId` 還是 client temp string id（offline / 尚未 reconcile）→ 直接 enqueue，待 `ensureConversation` 拿到 numeric id 後 `resolveTempId` replay。
4. Window focus 觸發 `flushAll`，把 retry queue 裡所有可送的 entry 重打。

對應檔：`runtime/classifyRetryQueue.js`（sessionStorage 為 backing store）。後端的 `POST /api/conversations/:id/declassify` endpoint 已在 Phase K 移除 — 平台對 classified 的 invariant 是真的單向。

---

## Release Notes

### Sprint 13 — Agentic loop UI surfaces（2026-05-03）

對應 anila-core **v0.12.0**。前端把 Sprint 9-12 累積的 typed event 全部視覺化，並加 dev tooling。

- **Runtime（`runtime/`）**：
  - `sse.js` — `dispatchSseEvent` 抽出來成 export，新增 7 個 callback（`onInterrupt` / `onResumed` / `onTodos` / `onFollowUps` / `onToolCallStarted` / `onToolCallFinished` / `onSpans`）+ `onUnknownEvent` 兜底；`streamChatCompletion` 多回拋 `X-Anila-Session-Id` 給 `onSessionId`；新增 `streamSessionAnswer` resume helper。
  - `api.js` — 新 `getSessionState(sid)` / `submitSessionAnswer` helper（pure JSON twin，主要給測試）。
  - `messageMeta.js` — `buildPersistMeta` 多 4 個累積欄位（`todos` / `tool_calls` / `spans` / `interrupt`），重載對話會還原同樣的 UI affordance。
- **元件**：
  - `agentic.jsx` — `<PausedBadge>`、`<InterruptCard>`（三個 kind 各自渲染：ask_user 帶 radio/checkbox + allow_other free-text、plan 帶 markdown plan + accept/decline、tool_approval 帶工具名 + JSON-formatted input preview + approve/deny）、`<TodoChecklist>`（可勾選樣式但唯讀，agent 端透過 `todo_write` 工具更新）、`<FollowUpChips>`（chip click → 父元件決定怎麼送）。
  - `toolExecution.jsx` — `<ToolExecutionWidget>` 依 `tool_name` 路由到 `TerminalOutput`（exec_bash/exec_python，黑底等寬 + status border）/ `DiffOutput`（apply_patch/file_edit，行首 +/- 著色）/ `FileTreeOutput`（glob/ls，JSON array 或 newline 任一）/ `PlainOutput`（fallback）。
  - `spanTree.jsx` — `<SpanTreeViewer>` 把後端 `InMemoryProcessor.to_tree()` 渲染成可摺疊的縮排樹。**Dev-only**，三種 opt-in：`localStorage.anila_dev=1` / `?devspans=1` / Vite `import.meta.env.DEV`。
- **測試**：新 `agentic.test.jsx` (21) / `toolExecution.test.jsx` (18) / `spanTree.test.jsx` (14) + 擴充 `sse.test.js` (+12 dispatch tests) / `messageMeta.test.js` (+5 typed-event 持久化 tests)。`vitest.setup.js` 加 `afterEach(cleanup)` 避免 jsdom 累積 sibling。總計 **120 tests pass**。
- **Setup 變更**：`vitest.setup.js` import `cleanup` from `@testing-library/react`，所有元件測試 between-test cleanup。

### Sprint 8 X — Phase K（2026-05-01）— Classified latch reload-escape hotfix

- `applyMeta()` 拿掉 `typeof convId === "number"` 死 guard 與 `.catch(() => {})` 吞失敗的問題；改成失敗 / temp-id 都 enqueue 到 `runtime/classifyRetryQueue.js`，window focus 觸發 flush，`ensureConversation` 拿到 numeric id 後 `resolveTempId` replay。修正使用者實測「重整 → 鎖被解除」的 P0 bug。
- 後端 `POST /api/conversations/:id/declassify` 整支 endpoint + service method 一起拔掉 — 違反平台 one-way latch invariant。如真有誤標需處理，走 admin manual ops + audit 而非 HTTP API。
- 11 個新 vitest case 覆蓋 retry queue（enqueue / replay / focus flush / 失敗 re-queue / corrupt sessionStorage 容錯）。

### Sprint 7 X — anila-ui API Key UI 全面下架（2026-04-27）

- 拔掉 `ApiKeyPopover` / `ApiKeyTab` / `maskApiKey` / 對應 icon imports；`streamChatCompletion` 的 legacy `apiKey` 參數同步移除。
- header 的 `sk-…` dropdown、Settings 的「API Key」tab、chat menu 的「API Key」項目全部不再存在 — 比保留死 UI 更安全：使用者無從填入 prod key 後得到「✓ 已儲存」的假成功訊息。
- 對應 commit：`0b8509e` / `0b22f54`。

### Sprint 5 X / 6 X — SSO 地基 + 全面資安修補（2026-04-27）

- OIDC：PKCE (S256) + nonce + JWKS 驗 `id_token`；`alg=none` 拒收；`email_verified` 強制；email-only 帳號合併禁止；`next_path` 集中 `sanitize_next_path` 擋 open-redirect。
- LDAP 完整下線（migration `0021` DROP `auth_providers.ldap_*`）。
- `users.local_password_disabled` flag（migration `0022`）讓 admin 對個別使用者切 SSO-only。
- Wave 2 cookie 流程接續鞏固：CSRF middleware 改 `hmac.compare_digest`；Bearer 路徑豁免 CSRF；nginx 6 個安全 header 全到位。

### Wave 2 — Auth / Session（2026-03）

- SPA 移除 localStorage JWT 與 sessionStorage API Key，完全改走 **httpOnly cookie + CSRF**。
- OIDC callback 不再發 short-lived API Key。
- 新增 `POST /api/auth/logout`。

### 2026-04-24 — AgenticRAG template 同步 + chat UI 改版

- Cross-reference 更新：`AgenticRAG` 為「**官方 RAG agent template**」。
- Chat bubble 重設計：Claude.ai-style flat rounded；Assistant 無頭列框、工具列 hover-reveal。
- `ReasoningSummary`：合併 routing trace + thinking 成單行 ghost row。
- Conversation sidebar：title 兩行 clamp；dropdown 改 `position: fixed` 避 overflow 切斷。
- 搜尋框 `×` clear button + Esc；tag 搜尋 + 同義詞展開（`特休` 可找到 `年假` / `HR`）。
- Composer：`@` autocomplete 下拉實際可用 agent；paste 優先 `text/*` 避免文字被 fallback 截圖當附件。
- EmptyState 改為單卡「ANILA 可以做什麼？」，prompt 由實際 agent 清單動態產出。
- Router 系統 prompt 新增「ambiguous → clarify」規則；`_normalize_clarify_bullets` 把 inline `·` 分隔的候選 agent 轉成 markdown bullet。

### 測試覆蓋

- Vitest: **80 tests**（69 原有 + Phase K 新增 11）。`messageMeta` / `titleClean` / `searchSynonyms` / `classified` / `sse` / `normalizeAgents` / `classifyRetryQueue`。

---

## 相關文件

- 平台整體：[`../../README.md`](../../README.md)
- CSP（本 UI 的 backend）：[`../../myCSPPlatform/README.md`](../../myCSPPlatform/README.md)
- Router（`anila-router` pseudo-agent 的實作端）：[`../../anila-core-router/README.md`](../../anila-core-router/README.md)
- **官方 RAG agent template**（可註冊被本 UI 分派）：[`../../AgenticRAG/README.md`](../../AgenticRAG/README.md)
- 路線圖：[`../../anila_plan.md`](../../anila_plan.md)
- Service-token cutover runbook：[`../../docs/runbooks/service-token-cutover.md`](../../docs/runbooks/service-token-cutover.md)

---

## License

見 repo 根 [`LICENSE`](../../LICENSE)。

---

**Last updated**: 2026-05-03 (Sprint 13 — agentic loop UI surfaces) · **Framework**: React + Vite · **Talks to**: CSP (`/api/*` + `/v1/*` cookie) + Router (`/v1/chat/completions` + `/v1/sessions/{id}/{state,answer}` cookie + `model=anila-router`) — both fronted by `nginx`
