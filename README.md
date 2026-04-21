# ANILA 平台

> **Runtime-first、On-prem 多 Agent 平台。** 三個服務、一個落地 LLM，docker compose 一鍵啟動。

ANILA 是一套企業內部的多 Agent 平台：統一管理模型與 API Key、對外以 OpenAI 相容介面提供推論、讓開發者基於樣板複製出自己的 Agent 並註冊進來、讓終端使用者透過統一 UI 與所有 Agent 對話，並以「主 LLM 未加密 → 遇到加密 agent 整段對話升級為加密」的單向閂鎖（one-way latch）處理敏感資料。

| 子專案 | 角色 | 預設 Port |
|---|---|---|
| [`myCSPPlatform`](./myCSPPlatform/) | **CSP**（Control & Data Plane）— 使用者 / API Key / 模型 / 對話 / 附件 / 分享 / 交接 / 審計 / OpenAI 相容代理 | `:8000` |
| [`anila-core`](./anila-core/) | **Runtime foundation（SDK）** — Python agent runtime 基座（api / registry / engine / tools / providers / storage / memory / compact / cli）。Router 與所有 agent 共用 | — |
| [`anila-core-router`](./anila-core-router/) | **Router** — 薄殼部署入口；OpenAI 相容分派器，依請求自動路由到註冊的 Agent | `:9000` |
| [`ANILA_UI/anila-ui`](./ANILA_UI/anila-ui/) | **Runtime UI** — React 聊天介面，串 CSP（JWT + SSE）與 Router（`anila-router` pseudo-agent） | `:3001`（compose）/ `:5173`（dev） |
| [`AgenticRAG`](./AgenticRAG/) | **RAG Sample Agent 樣板**。以 `anila-core` 為基座實作的 RAG agent 範例；開發者 fork 這裡當起點 | `:24786`（獨立執行時） |

> **唯一的規劃文件（single source of truth）**：[`anila_plan.md`](./anila_plan.md)。
> 根目錄 `onyx/` 不屬於 ANILA runtime，已排除於 compose stack 之外。

---

## 整體架構

```
                ┌─────────────────────────────────────────────┐
                │             使用者 / Agent 開發者           │
                └──────────────┬───────────────┬──────────────┘
                               │               │
                   瀏覽器      │               │  OpenAI SDK / curl
                   (JWT)       │               │  (Bearer sk-...)
                               ▼               ▼
                      ┌──────────────────────────────┐
                      │   ANILA Runtime UI (:3001)   │
                      │   React + Vite (SPA)         │
                      │   - 登入 / SSO                │
                      │   - 對話 / 分享 / 交接         │
                      │   - Agent 開發者儀表板         │
                      └────────┬────────────┬────────┘
                               │            │
                    /api/*     │            │   /v1/chat/completions
                    (JWT)      │            │   (API Key, SSE)
                               ▼            ▼
              ┌───────────────────────────────────────────┐
              │           myCSPPlatform (:8000)            │
              │  ┌─────────────────────────────────────┐  │
              │  │ Control Plane  /api/*  (JWT)        │  │
              │  │   auth / users / api_keys / models   │  │
              │  │   conversations / attachments /      │  │
              │  │   shares / handoffs / audit / alerts │  │
              │  ├─────────────────────────────────────┤  │
              │  │ Data Plane  /v1/*  (API Key)        │  │
              │  │   chat/completions (LLM/VLM/Agent)   │  │
              │  │   embeddings v1/v2                   │  │
              │  └─────────────────────────────────────┘  │
              └───────┬───────────────────┬────────────────┘
                      │                   │
        ┌─────────────┘                   └──────────────┐
        ▼                                                ▼
┌──────────────────┐                    ┌───────────────────────────────┐
│ PostgreSQL       │                    │  ANILA Router (:9000)         │
│ (csp schema)     │                    │  anila_core.api.router_server │
│  - users         │                    │  - /v1/chat/completions       │
│  - api_keys      │                    │    (auto-dispatch pseudo-     │
│  - model_        │                    │     agent: anila-router)      │
│    registry      │                    │  - /health  (cached_agents +  │
│  - conversations │                    │    last_refresh_error)        │
│  - token_usage   │                    └──────┬────────────────────────┘
│  - audit_logs    │                           │
└──────────────────┘                           │ 動態從 CSP /v1/agents
                                               │ 撈清單、依需求分派
                                               ▼
                                ┌─────────────────────────────────────┐
                                │ 已註冊 Agent（以 AgenticRAG 為樣板）│
                                │  - RAG 知識助理                      │
                                │  - Workflow agent                    │
                                │  - 自製 agent（開發者 fork 樣板）    │
                                │                                      │
                                │  全部透過 CSP 分派，認證用           │
                                │  CSP_SERVICE_TOKEN 做 s2s            │
                                └─────────────────┬───────────────────┘
                                                  │
                                                  ▼
                                      ┌───────────────────────┐
                                      │  落地 LLM / Embedding  │
                                      │  (vLLM / Ollama /      │
                                      │   TGI / llama.cpp...) │
                                      │  OpenAI 相容 endpoint  │
                                      └───────────────────────┘
```

**核心資料流（一次聊天請求）：**

```
使用者於 UI 送訊息
  │
  ├─ UI POST /v1/chat/completions  model="anila-router"  →  Router
  │
  │   Router 用 caller 的 Bearer API Key 呼叫 CSP:
  │     - GET  /v1/agents              取 agent manifest（requires_encryption 等）
  │     - POST /v1/chat/completions    主 LLM 判斷要不要分派
  │
  ├─ 主 LLM 回 "我需要叫 agent X"
  │     Router 以 caller 的 API Key 轉發到 agent X 的 endpoint_url
  │     agent X 內部可再呼叫 CSP /v1/* 拿 RAG、Embedding 等
  │
  ├─ Router / CSP 將 SSE 逐 chunk forward 回 UI
  │     同時在 meta 標注 classified=true（若 agent requires_encryption）
  │
  └─ UI 收到 classified=true → 對話永久閂鎖為加密模式（one-way latch，不可降級）
```

---

## 快速開始（compose 一鍵啟動）

### 1. 準備落地（on-prem）LLM

ANILA **不含雲端 LLM fallback，也不做 token/request quota**。把 `LOCAL_LLM_BASE_URL` 指向任何 OpenAI 相容 endpoint 即可：

| 後端 | `LOCAL_LLM_BASE_URL` | `LOCAL_LLM_MODEL` |
|---|---|---|
| 宿主機 Ollama | `http://host.docker.internal:11434/v1` | `llama3.2` |
| 叢集內 vLLM | `http://vllm.llm.svc.cluster.local:8000/v1` | 你部署時設定的模型名稱 |
| 本機 llama.cpp | `http://host.docker.internal:8080/v1` | `local-model` |

Embedding endpoint (`LOCAL_EMBEDDING_BASE_URL`) 若未設則預設等同 LLM URL。

### 2. 啟動整個 stack

```bash
# 於 repo 根目錄。需要 Docker + Compose v2。
export LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
export LOCAL_LLM_MODEL=llama3.2
export SMOKE_USER_API_KEY=sk-test-user-api-key   # 僅 smoke 用
export CSP_SERVICE_TOKEN=dev-service-token       # CSP ↔ Agent 的 s2s token
export CSP_SECRET_KEY=dev-secret-key-change-in-prod

docker compose up -d
```

啟動順序（由 healthcheck 串接）：`csp-db` → `csp` → `router` → `anila-ui`。首次啟動約 30 秒（含 alembic migration + 自動 seed smoke 使用者與 API Key）。

### 3. 驗證各服務

```bash
curl http://localhost:8000/health    # CSP
curl http://localhost:9000/health    # Router（會回報 cached_agents 與 last_refresh_error）
start http://localhost:3001          # UI (Windows) / macOS: open / Linux: xdg-open
```

> Router 的 `/health` 會暴露 `last_refresh_error`。若非 null 代表 Router 抓不到 CSP 的 agent 清單 — 通常是 `CSP_BASE_URL` 或 API Key 設錯。

### 4. Smoke test（真實打本地 LLM）

```bash
curl -N -X POST http://localhost:9000/v1/chat/completions \
  -H "Authorization: Bearer $SMOKE_USER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"anila-router","messages":[{"role":"user","content":"say hi"}],"stream":true}'
```

會看到 SSE chunk 從 `落地 LLM → CSP → Router → 你的終端` 逐段吐出。若已註冊 agent 且主 LLM 判定該分派，Router 會把 agent 自身的 SSE stream 即時 forward 回來。

---

## 本地開發（不使用 Docker）

每個服務可獨立跑，各讀自己的 env（詳見各子專案 README）：

```bash
# 1) CSP
cd myCSPPlatform
cp .env.example .env   # 改 SECRET_KEY / ADMIN_PASSWORD / AUTO_REGISTER_MODELS
./start.sh up          # 或 uv run uvicorn backend.app.main:app --reload --port 8000

# 2) Router
cd anila-core-router
pip install -e "../anila-core"          # pure runtime（不需要 RAG extras）
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --reload --port 9000

# 3) UI
cd ANILA_UI/anila-ui
cp .env.example .env.local
npm install && npm run dev   # :5173
```

---

## 專案結構

```
ANILA/
├── myCSPPlatform/        # CSP：FastAPI + SQLAlchemy + Alembic + Vue 管理 UI
├── anila-core/           # Runtime foundation（SDK）：api / registry / engine / tools / providers / ...
├── anila-core-router/    # Router 薄殼（main.py = create_router_app()；image 不含 RAG extras）
├── AgenticRAG/           # RAG sample agent 樣板（api.py；開發者 fork 起點）
├── ANILA_UI/anila-ui/    # React Runtime UI
├── docker-compose.yml    # CSP + Router + UI + PostgreSQL
├── anila_plan.md         # 單一事實來源：決策、Wave 計畫、架構
├── frontend_plan.md      # UI 設計決策
└── README.md             # 本檔
```

---

## 環境變數速查

| 變數 | 使用者 | 用途 |
|---|---|---|
| `LOCAL_LLM_BASE_URL` | CSP | 落地 LLM 的 OpenAI 相容 endpoint |
| `LOCAL_LLM_MODEL` | CSP, Router | 上面 endpoint 服務的模型名稱 |
| `LOCAL_EMBEDDING_BASE_URL` | CSP | Embedding endpoint（未設則同 LLM） |
| `LOCAL_EMBEDDING_MODEL` | CSP | Embedding 模型名稱 |
| `CSP_SECRET_KEY` | CSP | JWT 簽署密鑰 — 上線務必輪換 |
| `CSP_SERVICE_TOKEN` | CSP + Agent | Service-to-service header，由 `CspServiceTokenMiddleware` 驗 |
| `SMOKE_USER_API_KEY` | CSP | 自動 seed 的 smoke 使用者 API Key（僅 dev） |
| `ANILA_PUBLIC_CSP_BASE_URL` | UI build | 瀏覽器用來打 CSP 的對外 URL |
| `ANILA_PUBLIC_ROUTER_BASE_URL` | UI build | 瀏覽器用來打 Router 的對外 URL |

---

## 安全設計要點

- **On-prem runtime-first**：所有 LLM 流量進自家落地 endpoint，無雲端 fallback、無 token/request quota（Wave 0 已完整移除 quota/rate-limit 子系統）。
- **Service-to-service 認證**：Agent 驗 `CSP_SERVICE_TOKEN`。`AgenticRAG` 樣板的 middleware **import 失敗會 fail-fast**（Wave A 硬化），不再 silent fallback 成 no-op。
- **Classified 單向閂鎖**：只要 agent `requires_encryption=true` 或 SSE meta 帶 `classified=true`，CSP + Router + UI 三層都把該對話鎖成 classified；**UI 側無任何降級路徑**。
- **密鑰儲存**：API Key 放 `sessionStorage`（單 tab 作用域）、JWT 放 `localStorage`。OIDC SSO callback 會在後端自動發一把 24h 短效 API Key 回帶 UI（Wave A）。
- **審計日誌**：所有 admin 管理操作（建立 / 停用 / 改密碼 / 刪除 agent / 刪除 model 等）自動寫 `audit_logs`。

---

## 授權

見 [`LICENSE`](./LICENSE)。`onyx/` 子樹保留其原授權，**不屬於 ANILA**。
