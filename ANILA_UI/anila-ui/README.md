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

> ⚠️ **Wave 2 Auth 重構同步提醒**：myCSPPlatform 的 Wave 2 把 SPA session 改為 **httpOnly cookie**（`anila_access_token` / `anila_refresh_token` / `anila_csrf`），SPA 不再 localStorage / sessionStorage 保存 token。本 README 下方「開發備忘 → 登入流程（本機）」敘述沿用舊版寫法，實作細節以當前 `src/runtime/auth.jsx` + `src/runtime/api.js` 為準。

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

## Release Notes

### 2026-04-24 — AgenticRAG template 同步 + chat UI 改版

- Cross-reference 更新：`AgenticRAG` 為「**官方 RAG agent template**」
- Chat bubble 重設計：Claude.ai-style flat rounded；Assistant 無頭列框、工具列 hover-reveal
- `ReasoningSummary`：合併 routing trace + thinking 成單行 ghost row
- Conversation sidebar：title 兩行 clamp；dropdown 改 `position: fixed` 避 overflow 切斷
- 搜尋框 `×` clear button + Esc；tag 搜尋 + 同義詞展開（`特休` 可找到 `年假` / `HR`）
- Composer：`@` autocomplete 下拉實際可用 agent；paste 優先 `text/*` 避免文字被 fallback 截圖當附件
- EmptyState 改為單卡「ANILA 可以做什麼？」，prompt 由實際 agent 清單動態產出
- Router 系統 prompt 新增「ambiguous → clarify」規則；`_normalize_clarify_bullets` 把 inline `·` 分隔的候選 agent 轉成 markdown bullet

### 2026-03 — Auth / Session Wave 2

- SPA 移除 localStorage JWT 與 sessionStorage API Key，完全改走 **httpOnly cookie + CSRF**
- OIDC callback 不再發 short-lived API Key
- 新增 `POST /api/auth/logout`

### 測試覆蓋（2026-04）

- Vitest: **69 tests**（新增 `messageMeta` / `titleClean` / `searchSynonyms`）

---

## 相關文件

- 平台整體：[`../../README.md`](../../README.md)
- CSP（本 UI 的 backend）：[`../../myCSPPlatform/README.md`](../../myCSPPlatform/README.md)
- Router（`anila-router` pseudo-agent 的實作端）：[`../../anila-core-router/README.md`](../../anila-core-router/README.md)
- **官方 RAG agent template**（可註冊被本 UI 分派）：[`../../AgenticRAG/README.md`](../../AgenticRAG/README.md)
- 路線圖：[`../../anila_plan.md`](../../anila_plan.md) · UI 設計決策：[`../../frontend_plan.md`](../../frontend_plan.md)

---

## License

見 repo 根 [`LICENSE`](../../LICENSE)。

---

**Last updated**: 2026-04-24 · **Framework**: React + Vite · **Talks to**: CSP (:8000) + Router (:9000)
