# ANILA Runtime UI (`anila-runtime-ui`)

> ANILA 平台的前端 Runtime（React + Vite，version 1.0.0）：終端使用者登入後與 agent 對話、分享、交接、上傳附件，並可透過 `anila-router` pseudo-agent 讓主 LLM 自動分派。

> English version：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 UI 存在於所有 ANILA 部署分支。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。**登入路徑依分支而定**：`main` 等多數分支的 `login.jsx` 為**純本機帳密**（此 build 已移除 LDAP / OIDC / SSO 入口）；`prod-intranet-card` 的 `login.jsx` 已移除，改走 csp-governance-ui 的 Vue `LoginView.vue` 統一憑證卡 / SSO / 帳密入口；`trial-military` 精簡版視需求移除部分進階畫面。

---

## 簡介

`anila-ui`（package 名 `anila-runtime-ui`）是 ANILA 對外的**單頁前端應用（SPA）**。它不持有業務邏輯或模型，只負責：登入 / session 管理（httpOnly cookie + double-submit CSRF，無前端 token 儲存）、對話建立 / 瀏覽 / persist + SSE 串流渲染、agent 選擇 / 並排比對 / 分享 / 交接 / 附件、把後端 typed SSE 事件（trace / interrupt / todos / tool call / spans）視覺化。

平台整體架構、compose、環境變數見 repo 根 [`README.md`](../../README.md) 與路線圖 [`anila_plan.md`](../../anila_plan.md)。

---

## 架構與技術棧（讀自 `package.json`）

| 類別 | 內容 |
|---|---|
| 框架 / 路由 | **React 18.3.1** · `react-router-dom` 6.30.1 |
| 建置 | **Vite 6.3.5**（`@vitejs/plugin-react` 4.4.1） |
| Markdown / 數學 / 高亮 | `react-markdown` 9 + `remark-gfm` 4 / `remark-math` 6 + `rehype-katex` 7 / `rehype-highlight` 7 + `katex` 0.16 + `highlight.js` 11 |
| 圖表 | **`mermaid` 11.15.0**（流程圖渲染） |
| 測試 | **Vitest 3.1.3** + `@testing-library/react` 16 + `jest-dom` 6 + `jsdom` 26 |

scripts：`dev`（vite）/ `build`（vite build）/ `preview` / `test`（vitest run）。**無 `lint` script**。樣式：無 `src/styles.css`、無 Tailwind import，全 UI 仰賴 inline 元件樣式 + `index.html` 內含 base CSS。

---

## 目錄結構

```
apps/anila-shell/            # 前身 ANILA_UI/anila-ui
├── index.html · vite.config.js · vitest.setup.js
├── Dockerfile              # 多階段：node:22-alpine build（npm install）→ nginx:1.27-alpine serve
├── .env.example · docker/nginx.conf · docs/ · e2e/   # e2e/ 僅存歷史 README（Functions v1 stack 已整個移除，無 spec、無 playwright 依賴）
└── src/
    ├── main.jsx            # ReactDOM 入口；BrowserRouter(basename=import.meta.env.BASE_URL)
    │                       #   + AuthProvider + ConfirmProvider；/login、/app/*(RequireAuth)
    ├── app.jsx             # ChatRuntime — agent 選擇、送訊息、persist、orchestration
    ├── chat.jsx            # Sidebar / MessageBubble / Composer
    ├── collab.jsx          # ShareDialog / HandoffMenu / TagEditor
    ├── trust.jsx           # CitationsDrawer / ConfidentialWatermark
    ├── multiagent.jsx      # ParallelCompareView（2-3 agent 並排）
    ├── agentic.jsx         # InterruptCard / TodoChecklist / FollowUpChips / PausedBadge
    ├── toolExecution.jsx   # ToolExecutionWidget + Terminal/Diff/FileTree/Plain
    ├── spanTree.jsx        # SpanTreeViewer（dev trace tree）
    ├── login.jsx           # Login 頁（本機帳密；此 build 已移除 OIDC/SSO）
    ├── markdown.jsx        # ReactMarkdown + KaTeX / highlight.js（含 MarkdownImage + ImageLightbox）
    ├── banners.jsx         # BannerBar 公告列（dismiss 存 localStorage）
    ├── changelog.jsx       # 「What's New」modal（build-time，CHANGELOG_VERSION）
    ├── confirm.jsx         # ConfirmProvider + useConfirm / useToast（取代原生 confirm/alert）
    ├── components.jsx · icons.jsx · tweaks.jsx · data.jsx
    ├── runtime/            # 非 UI 邏輯
    │   ├── api.js          # fetch wrapper（credentials:include + CSRF）+ multipart + session helpers
    │   ├── auth.jsx · conversations.js · memory.js
    │   ├── sse.js          # SSE parser + dispatchSseEvent + streamChatCompletion + streamSessionAnswer
    │   ├── classified.js · classifyRetryQueue.js   # classified one-way latch + sessionStorage persist
    │   └── messageMeta.js · searchSynonyms.js · time.js · titleClean.js
    └── __tests__/          # Vitest（10 檔：agentic / classified / classifyRetryQueue / messageMeta /
                            #   normalizeAgents / searchSynonyms / spanTree / sse / titleClean / toolExecution）
```

> 測試在 **`src/__tests__/`**（非 `runtime/__tests__/`）。
>
> ⚠️ `e2e/README.md` 為**過時殘留**：它描述的 Functions v1 Playwright stack（`functions.spec.js`、sandbox/egress compose、`http://localhost:3001`）已被移除；本子專案目前**無 Playwright E2E**，請勿照該檔執行。

---

## 啟動與部署

```bash
cd apps/anila-shell
cp .env.example .env.local      # 若 CSP / Router 不在 localhost 則編輯
npm install && npm run dev      # Vite dev server :5173
```

Docker（多階段 `node:22-alpine` build → `nginx:1.27-alpine` serve，`EXPOSE 80`、`/health` 回 `ok`）：

```bash
docker build \
  --build-arg VITE_CSP_BASE_URL=http://csp.example:8000 \
  --build-arg VITE_ROUTER_BASE_URL=http://router.example:9000 \
  --build-arg BASE_PATH=/anila/ -t anila-runtime-ui .
docker run -p 8080:80 anila-runtime-ui
```

> 本子專案 `Dockerfile` 的 build-arg 預設為 `VITE_CSP_BASE_URL=http://localhost:8000`、`VITE_ROUTER_BASE_URL=http://localhost:9000`、`BASE_PATH=/`。repo 根 compose 會以 override 把它們設成 same-origin / `/router` 並由主 nginx 反向代理對外（預設 `https://localhost:4443/`）。

### 測試

```bash
npm test          # vitest run，跑 src/__tests__/ 的 10 個單元測試（runtime 純邏輯 + 少數元件）
```

僅有 Vitest 單元測試；本子專案目前無 E2E（見上方 layout 對 `e2e/` 的註記）。

---

## 與其他服務的關係

| 對象 | 路徑 | 認證 |
|---|---|---|
| **CSP Control Plane** | `/api/*` | cookie session（httpOnly + CSRF，401 自動 refresh） |
| **CSP Data Plane** | `/v1/*` | 同上 cookie；SDK / curl 另可 `Authorization: Bearer sk-…` |
| **ANILA Router** | `/v1/sessions/{id}/{state,answer}` | 同 CSP cookie；`model=anila-router` |

環境變數（`.env.example`，Vite build/dev time inline）：`VITE_CSP_BASE_URL`（預設 `http://localhost:8000`）、`VITE_ROUTER_BASE_URL`（預設 `http://localhost:9000`）。`BASE_PATH` 為 build-arg（非 `VITE_`），`vite.config.js` 讀 `BASE_PATH || VITE_BASE_PATH || '/'`。

實際呼叫的後端端點（取自 `runtime/*.js`）：`/api/conversations`（含 `/search`、`/{id}`、`/messages`、`/messages/{mid}/{rating,edit}`、`/shares`、`/{id}/classify`）、`/api/attachments`（multipart）、`/api/handoffs`（含 `/{id}/{accept,reject,cancel}`）、`/api/auth/refresh`、`/api/agents/{ref}/functions`、`/api/users/me/ui-settings`、`/api/banners/active`、`/api/memory/*`；streaming `POST /v1/chat/completions`（SSE，帶 `X-CSRF-Token`、數字會話帶 `X-ANILA-Conversation-Id`、讀回 `X-Anila-Session-Id`）；Router `GET /v1/sessions/{id}/state`、`POST /v1/sessions/{id}/answer`（SSE resume）。

**Classified 規則由後端決定**：agent `requires_encryption=true` 或 SSE meta `classified=true` 時，對話 one-way latch 為 classified，UI 無降級介面（latch persist 由 `runtime/classifyRetryQueue.js` 確保抵達 CSP）。

---

## 相關文件

- 平台整體：[`../../README.md`](../../README.md)、路線圖 [`../../anila_plan.md`](../../anila_plan.md)、分支策略 [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- CSP：[`../../services/csp/README.md`](../../services/csp/README.md) · Router：[`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md) · License：[`../../LICENSE`](../../LICENSE)

---

**Framework**：React + Vite · **Talks to**：CSP（`/api/*` + `/v1/*` cookie）+ Router（`/v1/sessions/*` cookie, `model=anila-router`）— both fronted by `nginx`.
