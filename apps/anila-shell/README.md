# ANILA Shell — 任務中心 Runtime（`anila-runtime-ui`）

> ANILA 的一般使用者介面（React + Vite，v1.0.0）。登入後在這裡與模型或助手對話、看用量與記憶、開啟已登記的專案入口，並從側欄進到知識庫。簡報、報告等產出在知識庫那一頁。

> English mirror: [`README.en.md`](./README.en.md)

- 平台現況見 [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md)。

---

## 1. 產品定位

側欄（`src/shellNav.jsx` 的 `buildShellEntries()`）是這五項：

```text
ANILA
├── 對話        ← 預設視圖
├── 我的知識庫  → /anilalm（知識庫與產出都在這一頁；開關關掉時這一列顯示「即將推出」、不能點）
├── 專案入口    → ServicesPanel
├── 用量
└── 記憶
```

owner、admin、developer，以及單位管理員，另外看得到「治理中心」（連到同源 `/`）。

本 Shell 不持有模型。它負責工作階段守衛、建立對話、把串流畫出來（trace、interrupt、todos、tool call、spans），並帶上分類浮水印、trace、審計。畫面上的標籤用產品用語，不出現 ANILALM、Studio、CSP。

### 無登入頁（統一 SSO）

`src/main.jsx` 只註冊 `/app/*`（`RequireAuth`）與 catch-all `Navigate → /app`。未認證時 `RequireAuth` 以**整頁導向**跳到治理中心的 `/login`（Vue `LoginView.vue`，服務於同源 443），並夾帶 `next` 讓登入後跳回。Shell 內**沒有** `login.jsx`。

---

## 2. 核心能力

| 能力 | 檔案 | 摘要 |
|---|---|---|
| **側欄** | `src/shellNav.jsx` | `buildShellEntries()` 產生對話、我的知識庫、專案入口、用量、記憶。`canSeeGovernance()`（owner、admin、developer，以及 `is_unit_admin`）才追加「治理中心」。`originHref()` 用 origin 絕對路徑連到 `/anilalm` 與 `/`。掛在 `src/chat.jsx` 側欄。 |
| **Task 主流程** | `src/runtime/tasks.js`、`src/runtime/sse.js`、`src/app.jsx` | 首次送訊息時 `createTaskForConversation()` → `POST /api/tasks`（`task_type:"query"`、`source_scope:"none"`），`taskId` 快取於對話狀態；`sse.js` 於串流帶 **`X-ANILA-Task-Id`** 讓 CSP 把派發掛回同一 Task。任何失敗回傳 `null`＋zh-TW `console.warn`，聊天以無任務模式照常運作。 |
| **Trace Explorer** | `src/runtime/traces.js`、`src/spanTree.jsx` | `fetchTrace()` → `GET /api/traces/{trace_id}` 取持久化 `{trace_id, task_id, spans[]}`；`spansToTree()` 把扁平 spans 組成樹；`TraceExplorer` 提供「**檢視軌跡**」控制並以 `SpanTreeViewer` 呈現。取軌跡失敗降級為「尚無軌跡資料」，絕不崩潰。 |
| **四級機敏分類** | `src/trust.jsx`、`src/runtime/classified.js`、`src/runtime/classifyRetryQueue.js` | `watermarkLevel()` 由對話狀態推出中文級別。`ClassificationWatermark` 是角標（密＝warn、機密＝danger-strong；無機密與營業秘密不上全螢幕浮水印）。`ConfidentialWatermark` 全螢幕斜向重複「密等 · 讀取者 · 讀取時間（到分鐘）」，換對話才重算時間。閂鎖由伺服器決定，`classifyRetryQueue` 負責送到 CSP。 |
| **專案入口 ServicesPanel** | `src/services.jsx` | `fetchServices()` → `GET /api/services`；該路由回 404 時改走 `GET /api/platform-links`。`resolveLaunch()` 對 registry 服務走 `POST /api/services/{id}/launch`。`new_tab` → `window.open(_,'_blank','noopener')`；`iframe` → 站內沙箱覆蓋層（`sandbox="allow-scripts allow-same-origin allow-forms"`、`referrerPolicy="no-referrer"`）＋「此服務由 <name> 提供」。 |

---

## 3. 技術棧（讀自 `package.json`）

| 類別 | 內容 |
|---|---|
| 框架 / 路由 | **React 18.3.1** · `react-router` 7.18.2（`>=7.18.2 <8`） |
| 建置 | **Vite 6.3.5**（`@vitejs/plugin-react` 4.4.1） |
| Markdown / 數學 / 高亮 | `react-markdown` 9 + `remark-gfm` 4 / `remark-math` 6 + `rehype-katex` 7 + `rehype-highlight` 7 + `katex` 0.16 + `highlight.js` 11 |
| 圖表 | **`mermaid` 11.16.1** |
| 測試 | **Vitest 3.1.3** + `@testing-library/react` 16 + `jest-dom` 6 + `jsdom` 26 |

`scripts`：`dev` / `build` / `preview` / `test`（`vitest run`）。**無 `lint` script**。無 Tailwind／全域 stylesheet — UI 走 inline 元件樣式 + `index.html` 內含 base CSS 與 `--font-sans/--font-mono` 系統字型堆疊（air-gap，不載外部字型）。

---

## 4. 目錄結構

```
apps/anila-shell/
├── index.html · vite.config.js · vitest.setup.js
├── Dockerfile              # node:22-alpine build（npm ci）→ nginx:1.30.4-alpine（釘 digest）serve；EXPOSE 80
├── .env.example · docker/nginx.conf · docs/
└── src/
    ├── main.jsx            # 入口；BrowserRouter(basename=BASE_URL) + AuthProvider + ConfirmProvider；僅 /app/*，無登入頁
    ├── app.jsx             # ChatRuntime — agent 選擇、送訊息、Task 建立、Trace Explorer、分類浮水印、ServicesPanel 掛載
    ├── shellNav.jsx        # 四入口 ShellNav + admin-gated 治理中心（originHref 同源連結）
    ├── chat.jsx            # Sidebar（掛 ShellNav）/ MessageBubble / Composer
    ├── services.jsx        # ServicesPanel（fetchServices / resolveLaunch / IframeOverlay）
    ├── trust.jsx           # 引用抽屜 / 信心 / ClassificationWatermark / ConfidentialWatermark / AuditWatermark
    ├── spanTree.jsx        # spansToTree + SpanTreeViewer + TraceExplorer（檢視軌跡）
    ├── collab.jsx · multiagent.jsx · agentic.jsx · toolExecution.jsx · markdown.jsx · banners.jsx · changelog.jsx · confirm.jsx · components.jsx · icons.jsx · tweaks.jsx · data.jsx
    ├── runtime/            # 非 UI 邏輯：api.js（fetch + CSRF）· auth.jsx · conversations.js · memory.js ·
    │                       #   sse.js（SSE parser + X-ANILA-Task-Id）· tasks.js · traces.js ·
    │                       #   classified.js · classifyRetryQueue.js · messageMeta.js · searchSynonyms.js · time.js · titleClean.js
    └── __tests__/          # Vitest（17 檔）
```

---

## 5. 啟動、部署與測試

### 本機開發

```bash
cd apps/anila-shell
cp .env.example .env.local      # CSP / Router 非 localhost 時才需編輯
npm install && npm run dev      # Vite dev server :5173
```

### 容器（monorepo compose）

本 Shell 在 compose 中的 service 名為 **`anila-ui`**（build context `apps/anila-shell`），由根目錄 compose shim 納管：`compose.yaml`（`name: anila`）→ `infra/compose/platform.yml`，dev 為 `compose.dev.yaml` → `infra/compose/dev.yml`。正式部署以 `BASE_PATH=/anila/` build，經主 nginx 於同源 443 `/anila/` 反向代理（SSO cookie 自然共用）。

```bash
docker compose -f compose.yaml up -d anila-ui        # 隨全棧一起 build/up
# 日常生命週期（status/logs/restart）走 infra/deployment/scripts/deploy-prod.sh
```

### 測試

```bash
npm test
```

僅 Vitest 單元測試；本子專案沒有 Playwright E2E。

---

## 6. 後端契約與環境變數

| 對象 | 路徑 | 認證 |
|---|---|---|
| CSP 控制面 | `/api/*` | 同源 cookie session（httpOnly + double-submit CSRF，401 自動 refresh） |
| CSP 資料面 | `/v1/*` | 同上 cookie；SDK／curl 另可 `Authorization: Bearer sk-…` |
| ANILA Router | `/v1/sessions/{id}/{state,answer}` | 同 CSP cookie；`model=anila-router` |

環境變數（`.env.example`，Vite build/dev time inline）：`VITE_CSP_BASE_URL`（預設 `http://localhost:8000`）、`VITE_ROUTER_BASE_URL`（預設 `http://localhost:9000`）。`BASE_PATH` 為 build-arg（非 `VITE_`），`vite.config.js` 讀 `BASE_PATH || VITE_BASE_PATH || '/'`。

實際呼叫端點（取自 `src/runtime/*.js` 與元件）：`/api/tasks`、`/api/traces/{id}`、`/api/services`（+ `/{id}/launch`）、`/api/platform-links`、`/api/conversations*`、`/api/attachments`、`/api/handoffs*`、`/api/auth/refresh`、`/api/agents/{ref}/functions`、`/api/banners/active`、`/api/memory/*`；串流 `POST /v1/chat/completions`（帶 `X-CSRF-Token`、`X-ANILA-Task-Id`、`X-ANILA-Conversation-Id`）；Router `GET /v1/sessions/{id}/state`、`POST /v1/sessions/{id}/answer`。

---

## 7. 相關文件

- 平台整體：[`../../README.md`](../../README.md)
- 平台現況見 [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md)。
- 相鄰入口：知識庫／產出中心 [`../anilalm/README.md`](../anilalm/README.md) · 治理中心 [`../csp-governance-ui/README.md`](../csp-governance-ui/README.md)
- 後端：[`../../services/csp/README.md`](../../services/csp/README.md) · Router [`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)

