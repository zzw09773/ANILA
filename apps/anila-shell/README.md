# ANILA Shell — 任務中心 Runtime（`anila-runtime-ui`）

> ANILA 平台的終端使用者前端（React + Vite，v1.0.0）。使用者登入後在此**提出任務、與模型／Agent 對話、檢視軌跡、啟動專案入口服務**。這是 ANILA 唯一的一般使用者入口殼層（Shell），承載「任務中心」預設視圖，並以同源導覽連到「我的知識庫／產出中心／專案入口」。

> English mirror: [`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 UI 存在於所有 ANILA 部署分支；分支策略見根目錄 [`README.md`](../../README.md) 與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。**登入已統一交給治理中心**：本 Shell 不再持有登入頁（見下方「無登入頁」）。
>
> 設計權威：[`docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)（憲章）、[`10-migration-and-development-guardrails.md`](../../docs/anila-redesign-docs/10-migration-and-development-guardrails.md)（§11 Shell IA）、[`12-frontend-visual-redesign.md`](../../docs/anila-redesign-docs/12-frontend-visual-redesign.md)（分類浮水印）。

---

## 1. 產品定位

依產品憲章（doc 00 §2），正式使用者只看到 **ANILA** 一個產品，四個一級入口：

```text
ANILA
├── 任務中心    ← 本 Shell 的預設視圖（chat = 任務工作台）
├── 我的知識庫  → 同源知識 SPA（/anilalm）
├── 產出中心    → 同源知識 SPA 的 Studio 面（/anilalm）
└── 專案入口    → ServicesPanel（Registry 服務卡片）
```

本 Shell **不持有業務邏輯或模型**，只負責：工作階段守衛、對話建立／串流渲染、把後端 typed SSE 事件（trace／interrupt／todos／tool call／spans）視覺化，並串起 Task 主流程與治理底座（分類浮水印、trace、審計）。標籤一律用產品語彙，不對使用者暴露 ANILALM／Studio／CSP 等技術品牌名（`src/shellNav.jsx`）。

### 無登入頁（統一 SSO）

`src/main.jsx` 只註冊 `/app/*`（`RequireAuth`）與 catch-all `Navigate → /app`。未認證時 `RequireAuth` 以**整頁導向**跳到治理中心的 `/login`（Vue `LoginView.vue`，服務於同源 443），並夾帶 `next` 讓登入後跳回。Shell 內**沒有** `login.jsx`。

---

## 2. 核心能力（對應 redesign Slices）

| 能力 | 檔案 | 摘要 |
|---|---|---|
| **四入口 ShellNav** | `src/shellNav.jsx` | `buildShellEntries()` 產生四大入口；`canSeeGovernance()`（owner／admin／developer）才追加「治理中心」；`originHref()` 以 origin 絕對路徑連外部同源介面。掛載於 `src/chat.jsx` 側欄（展開＋收合兩態）。 |
| **Task 主流程** | `src/runtime/tasks.js`、`src/runtime/sse.js`、`src/app.jsx` | 首次送訊息時 `createTaskForConversation()` → `POST /api/tasks`（`task_type:"query"`、`source_scope:"none"`），`taskId` 快取於對話狀態；`sse.js` 於串流帶 **`X-ANILA-Task-Id`** 讓 CSP 把派發掛回同一 Task。任何失敗回傳 `null`＋zh-TW `console.warn`，聊天以無任務模式照常運作。 |
| **Trace Explorer** | `src/runtime/traces.js`、`src/spanTree.jsx` | `fetchTrace()` → `GET /api/traces/{trace_id}` 取持久化 `{trace_id, task_id, spans[]}`；`spansToTree()` 把扁平 spans 組成樹；`TraceExplorer` 提供「**檢視軌跡**」控制並以 `SpanTreeViewer` 呈現。取軌跡失敗降級為「尚無軌跡資料」，絕不崩潰。 |
| **四級機敏分類** | `src/trust.jsx`、`src/runtime/classified.js`、`src/runtime/classifyRetryQueue.js` | `watermarkLevel()` 由對話狀態推導真實中文級別；`ClassificationWatermark` 為**真四級角標**（密＝warn／機密＝danger-strong；無機密／營業秘密不上全浮水印），取代裝飾性英文 CONFIDENTIAL；`ConfidentialWatermark` 為**全螢幕鑑識浮水印**，對角平鋪帶「密等 · 讀取者 · 讀取時間（分鐘精度）」以供截圖外洩溯源（換對話才重凍時間）。單向閂鎖由後端決定，`classifyRetryQueue` 確保 latch 抵達 CSP。 |
| **專案入口 ServicesPanel** | `src/services.jsx` | `fetchServices()` → `GET /api/services`，7a 後端未上線（404）退回 `GET /api/platform-links`（legacy）；`resolveLaunch()` 對 registry 服務走 `POST /api/services/{id}/launch`。`new_tab` → `window.open(_,'_blank','noopener')`；`iframe` → 站內沙箱覆蓋層（`sandbox="allow-scripts allow-same-origin allow-forms"`、`referrerPolicy="no-referrer"`）＋「此服務由 <name> 提供」安全提示。 |

---

## 3. 技術棧（讀自 `package.json`）

| 類別 | 內容 |
|---|---|
| 框架 / 路由 | **React 18.3.1** · `react-router` 7.18.0 |
| 建置 | **Vite 6.3.5**（`@vitejs/plugin-react` 4.4.1） |
| Markdown / 數學 / 高亮 | `react-markdown` 9 + `remark-gfm` 4 / `remark-math` 6 + `rehype-katex` 7 + `rehype-highlight` 7 + `katex` 0.16 + `highlight.js` 11 |
| 圖表 | **`mermaid` 11.15.0** |
| 測試 | **Vitest 3.1.3** + `@testing-library/react` 16 + `jest-dom` 6 + `jsdom` 26 |

`scripts`：`dev` / `build` / `preview` / `test`（`vitest run`）。**無 `lint` script**。無 Tailwind／全域 stylesheet — UI 走 inline 元件樣式 + `index.html` 內含 base CSS 與 `--font-sans/--font-mono` 系統字型堆疊（air-gap，不載外部字型）。

---

## 4. 目錄結構

```
apps/anila-shell/
├── index.html · vite.config.js · vitest.setup.js
├── Dockerfile              # node:22-alpine build（npm ci）→ nginx:1.30.4-alpine（釘 digest）serve；EXPOSE 80
├── .env.example · docker/nginx.conf · docs/ · e2e/（僅歷史 README，無 spec）
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

本 Shell 在 compose 中的 service 名為 **`anila-ui`**（build context `apps/anila-shell`），由根目錄 compose shim 納管：`compose.yaml`（`name: anila-platform`）→ `infra/compose/platform.yml`，dev 為 `compose.dev.yaml` → `infra/compose/dev.yml`。正式部署以 `BASE_PATH=/anila/` build，經主 nginx 於同源 443 `/anila/` 反向代理（SSO cookie 自然共用）。

```bash
docker compose -f compose.yaml up -d anila-ui        # 隨全棧一起 build/up
# 日常生命週期（status/logs/restart）走 infra/deployment/scripts/deploy-prod.sh
```

### 測試

```bash
npm test        # vitest run — 17 檔 / 222 個測試（截至撰稿全綠）
```

僅 Vitest 單元測試；本子專案目前無 Playwright E2E（`e2e/README.md` 為過時殘留，勿依它執行）。

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

- 平台整體：[`../../README.md`](../../README.md) · 分支策略 [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- 設計權威：[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)（憲章 00 / IA 10 / 視覺 12 / 契約 09）
- 相鄰入口：知識庫／產出中心 [`../anilalm/README.md`](../anilalm/README.md) · 治理中心 [`../csp-governance-ui/README.md`](../csp-governance-ui/README.md)
- 後端：[`../../services/csp/README.md`](../../services/csp/README.md) · Router [`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)

---

**Framework**：React + Vite · **Serves**：任務中心（Shell IA）· **Talks to**：CSP（`/api/*` + `/v1/*` cookie）+ Router（`/v1/sessions/*`）— 皆經 `nginx` 同源前置。
