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

### 分享 / 交接 / 附件

```
分享：      ShareDialog → POST /api/conversations/{id}/shares
            → 回傳 /s/c/<token> 連結

交接：      HandoffMenu → POST /api/handoffs
            → 對方於 /handoffs 頁接受（accept）/ 拒絕（reject）

附件：      Composer onAttach → POST /api/attachments (multipart)
            → 回傳 attachment_id → 一併送進 /v1/chat/completions
```

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

1. 用 CSP 帳號登入（本機帳密或 OIDC）。
2. 登入後在右上角頭像旁邊的彈出視窗貼一把 CSP API Key。
   - OIDC 登入時，callback 會**自動**把 24h 短效 API Key 塞進來，不需要手動貼。
3. 即可開始對話。

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
├── login.jsx            # Login 頁（本機 + OIDC）
├── tweaks.jsx           # 視覺微調 panel（storyboard）
├── main.jsx             # ReactDOM 掛載入口
├── styles.css           # Tailwind + global styles
└── runtime/
    ├── api.js           # fetch wrapper + JWT 自動 refresh + multipart helper
    ├── auth.jsx         # AuthProvider + useAuth hook
    ├── conversations.js # CSP control-plane endpoint wrappers
    └── sse.js           # SSE parser（/v1/chat/completions）
```

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

**推薦使用 repo 根 [`docker-compose.yml`](../../docker-compose.yml)**，會一次拉起 `csp-db` + `csp` + `router` + `anila-ui`：

```bash
cd ../../
docker compose up -d
# UI: http://localhost:3001
```

---

## 開發備忘

### 登入流程（本機）

```
POST /api/auth/login
  { username, password }
    ↓
  { access_token, refresh_token, user }
    ↓
localStorage: access_token / refresh_token
sessionStorage: api_key（手動貼或 OIDC 自動帶回）
```

### JWT refresh

`runtime/api.js` 攔截 401，自動呼叫 `/api/auth/refresh`，成功後重送原請求；失敗則踢回 `/login`。

### SSE parsing

`runtime/sse.js` 解析 `data: {...}` 行，產生 `{ type, payload }` stream。支援：

| event | 用途 |
|---|---|
| `message_delta` | 文字片段 |
| `tool_call_started` / `tool_call_finished` | 工具呼叫（UI trace） |
| `meta` | 含 `classified`、`agent_id`、`citations` 等 |
| `usage_update` | token 使用量 |
| `stream_done` | 結束 |
| `error` | 錯誤 |

### Classified latch 實作

`app.jsx` 的 `applyMeta()`：只在收到 `classified=true` 時把 conversation 升級為 classified，**永不**從 `true` 降回 `false`。這是對應平台規格的 one-way 規則。

---

## License

見 repo 根 [`LICENSE`](../../LICENSE)。
