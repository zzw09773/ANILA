# ANILA Runtime UI (`anila-runtime-ui`)

> ANILA 平台的前端 Runtime（React + Vite）：終端使用者登入後與 agent 對話、分享、交接、上傳附件，並可透過 `anila-router` pseudo-agent 讓主 LLM 自動分派。

> English version：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 UI 存在於所有 ANILA 部署分支。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。**登入路徑依分支而定**：多數分支走本機帳密（`src/login.jsx` + `src/runtime/auth.jsx`）；`prod-intranet-card` 的 `src/login.jsx` 已移除，改走 myCSPPlatform 的 Vue `LoginView.vue` 統一憑證卡 / SSO / 帳密入口。`trial-military` 精簡版移除部分進階畫面（多 agent 並排比較等視需求）。

---

## 簡介

`anila-ui`（package 名 `anila-runtime-ui`）是 ANILA 對外的**單頁前端應用（SPA）**。它不持有業務邏輯或模型，只負責：

- 登入 / session 管理（httpOnly cookie + CSRF，無前端 token 儲存）。
- 對話的建立、瀏覽、persist，與 SSE 串流渲染。
- agent 選擇、並排比對、分享 / 交接 / 附件等協作操作。
- 把後端送來的 typed SSE 事件（trace、interrupt、todos、tool call、spans）視覺化。

平台整體架構、compose 啟動、環境變數見 repo 根 [`README.md`](../../README.md) 與路線圖 [`anila_plan.md`](../../anila_plan.md)；本檔聚焦 UI 子專案本身。

---

## 架構與技術棧

讀自 `package.json`（`name: anila-runtime-ui`、`type: module`）：

| 類別 | 內容 |
|---|---|
| 框架 | **React 18**（`react` / `react-dom` `^18.3`） |
| 路由 | `react-router-dom` `^6.30` |
| 建置 | **Vite 6**（`@vitejs/plugin-react`） |
| Markdown / 數學 / 高亮 | `react-markdown` + `remark-gfm` / `remark-math` + `rehype-katex` / `rehype-highlight` + `katex` + `highlight.js` |
| 測試 | **Vitest 3** + `@testing-library/react` + `jsdom` |

樣式：本 repo **沒有** `src/styles.css` 或 Tailwind import，全 UI 仰賴 inline 元件樣式 + `index.html` 內含的 base CSS。

---

## 目錄結構

```
anila-ui/
├── index.html              # SPA 入口 HTML（含 base CSS）
├── vite.config.js          # Vite + react plugin + vitest 設定
├── vitest.setup.js
├── Dockerfile              # 多階段：node:22 build → nginx:1.27 serve
├── .env.example            # VITE_* 環境變數範本
├── docker/nginx.conf       # runtime 階段 nginx SPA 設定（含 /health）
├── e2e/                    # E2E 說明
└── src/
    ├── main.jsx            # ReactDOM 掛載入口
    ├── app.jsx             # ChatRuntime — agent 選擇、送訊息、persist
    ├── chat.jsx            # Sidebar / MessageBubble / Composer
    ├── collab.jsx          # ShareDialog / HandoffMenu / TagEditor
    ├── trust.jsx           # CitationsDrawer / ConfidentialWatermark
    ├── multiagent.jsx      # ParallelCompareView（2-3 agent 並排）
    ├── agentic.jsx         # InterruptCard / TodoChecklist / FollowUpChips / PausedBadge
    ├── toolExecution.jsx   # ToolExecutionWidget + Terminal/Diff/FileTree/Plain
    ├── spanTree.jsx        # SpanTreeViewer（dev-only）
    ├── login.jsx           # Login 頁（本機帳密 + OIDC；prod-intranet-card 已移除改走 CSP Vue）
    ├── markdown.jsx        # ReactMarkdown 包裝 + KaTeX / highlight.js（含 MarkdownImage + ImageLightbox）
    ├── components.jsx · icons.jsx · tweaks.jsx · data.jsx
    └── runtime/            # 非 UI 邏輯
        ├── api.js          # fetch wrapper + cookie + CSRF + multipart + session helpers
        ├── auth.jsx        # AuthProvider + useAuth hook
        ├── conversations.js# CSP control-plane endpoint wrappers
        ├── memory.js       # /api/memory/* wrappers
        ├── sse.js          # SSE parser + dispatchSseEvent + resume helper
        ├── classified.js · classifyRetryQueue.js  # classified one-way latch + persist
        ├── messageMeta.js · searchSynonyms.js · time.js · titleClean.js
        └── __tests__/      # Vitest
```

---

## 啟動與部署

### 本機開發

```bash
cd ANILA_UI/anila-ui
cp .env.example .env.local      # 若 CSP / Router 不在 localhost 則編輯
npm install
npm run dev                     # Vite dev server :5173
```

打開 <http://localhost:5173>，首次進入重導 `/login`，用 CSP 帳號登入即可對話。

### Scripts

| 指令 | 作用 |
|---|---|
| `npm run dev` | Vite dev server（HMR）`:5173` |
| `npm run build` | Production build 到 `dist/` |
| `npm run preview` | 本機跑 production build |
| `npm test` | Vitest（`vitest run`） |

### Docker（單獨 build）

多階段：`node:22-alpine` build → `nginx:1.27-alpine` serve（`docker/nginx.conf`，`EXPOSE 80`、`/health` 回 `ok`）。

```bash
docker build --build-arg VITE_CSP_BASE_URL=http://csp.example:8000 \
  --build-arg VITE_ROUTER_BASE_URL=http://router.example:9000 -t anila-runtime-ui .
docker run -p 8080:80 anila-runtime-ui
```

### 與 CSP / Router 一起跑（compose）

`anila-ui` 定義於 repo 根 [`docker-compose.yml`](../../docker-compose.yml) 與 [`docker-compose-dev.yml`](../../docker-compose-dev.yml)：build context `ANILA_UI/anila-ui`，僅 `expose: 80`（無 host port），healthcheck `/health`，`depends_on` `csp` + `router`（service_healthy），對外經 `nginx` 反向代理。Build args 預設 `VITE_CSP_BASE_URL` 為空（→ same-origin 相對 URL）、`VITE_ROUTER_BASE_URL` 為 `/router`。

```bash
cd ../../ && docker compose up -d     # 一併拉起 csp-db / csp / redis / router / anila-ui / nginx
# UI 經 nginx 對外，預設 https://localhost:4443/
```

---

## 與其他服務的關係

| 對象 | 路徑 | 認證 |
|---|---|---|
| **CSP Control Plane** | `/api/*` | cookie session（httpOnly + CSRF，401 自動 refresh） |
| **CSP Data Plane** | `/v1/*` | 同上 cookie；SDK / curl 另可走 `Authorization: Bearer sk-…` |
| **ANILA Router** | `/v1/*`、`/v1/sessions/{id}/{state,answer}` | 同 CSP cookie；`model=anila-router` pseudo-agent |

環境變數（`.env.example`，Vite 於 build/dev time inline 進 client bundle）：

| 變數 | 用途 | 預設 |
|---|---|---|
| `VITE_CSP_BASE_URL` | CSP Control Plane（`/api/*`）+ Data Plane（`/v1/*`）基底 | `http://localhost:8000` |
| `VITE_ROUTER_BASE_URL` | ANILA Router 基底（`anila-router` pseudo-agent） | `http://localhost:9000` |

未設時 `src/runtime/api.js` 於 boot `console.warn`；空值 fallback 成相對路徑，僅在反向代理同時 front 兩服務時才正常。

主要後端端點：`GET/POST /api/conversations`（含 `{id}` / `/messages` / `/shares`）、`POST /api/attachments`（multipart）、`POST /api/handoffs`（含 `/accept` `/reject` `/cancel`）、`POST /v1/chat/completions`（SSE）、`/api/memory/*`。**Classified 規則由後端決定**：當 agent `requires_encryption=true` 或 SSE meta 帶 `classified=true`，對話 one-way latch 為 classified，UI 無降級介面（latch persist 由 `runtime/classifyRetryQueue.js` 確保抵達 CSP）。

---

## 相關文件

- 平台整體：[`../../README.md`](../../README.md)、路線圖 [`../../anila_plan.md`](../../anila_plan.md)、分支策略 [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- CSP（本 UI 的 backend）：[`../../myCSPPlatform/README.md`](../../myCSPPlatform/README.md)
- Router（`anila-router` pseudo-agent）：[`../../anila-core-router/README.md`](../../anila-core-router/README.md)
- Agent template：[`../../anila-agent/README.md`](../../anila-agent/README.md) · License：[`../../LICENSE`](../../LICENSE)

---

**Framework**：React + Vite · **Talks to**：CSP（`/api/*` + `/v1/*` cookie）+ Router（`/v1/*` cookie, `model=anila-router`）— both fronted by `nginx`.
