# ANILA 平台

> **Runtime-first、On-prem 多 Agent 平台。** 三個服務、一個落地 LLM，docker compose 一鍵啟動。

ANILA 是一套企業內部的多 Agent 平台：統一管理模型與 API Key、對外以 OpenAI 相容介面提供推論、讓開發者基於樣板複製出自己的 Agent 並註冊進來、讓終端使用者透過統一 UI 與所有 Agent 對話，並以「主 LLM 未加密 → 遇到加密 agent 整段對話升級為加密」的單向閂鎖（one-way latch）處理敏感資料。

| 子專案 | 角色 | 預設 Port |
|---|---|---|
| [`myCSPPlatform`](./myCSPPlatform/) | **CSP**（Control & Data Plane）— 使用者 / API Key / 模型 / 對話 / 附件 / 分享 / 交接 / 審計 / OpenAI 相容代理 | `:8000` |
| [`anila-core`](./anila-core/) | **Runtime foundation（SDK）** — Python agent runtime 基座（api / registry / engine / tools / providers / storage / memory / compact / cli）。Router 與所有 agent 共用 | — |
| [`anila-core-router`](./anila-core-router/) | **Router** — 薄殼部署入口；OpenAI 相容分派器，依請求自動路由到註冊的 Agent | `:9000` |
| [`ANILA_UI/anila-ui`](./ANILA_UI/anila-ui/) | **Runtime UI** — React 聊天介面，串 CSP（JWT + SSE）與 Router（`anila-router` pseudo-agent） | `:3001`（compose）/ `:5173`（dev） |
| [`AgenticRAG`](./AgenticRAG/) | **RAG Agent Template**（官方）— 完整 framework（tool-driven loop、hybrid search、cross-encoder reranker、vision pipeline、compact/memory）。以 `anila-core` 為基座；開發者 fork 這裡當起點 | `:24786`（獨立執行時） |
| [`runtime_logic`](./runtime_logic/) | **TS Runtime 參考材料**（READ-ONLY）— 用來對照移植到 `anila-core` 的 agent runtime 設計原本；原始碼 gitignored | — |

> **唯一的規劃文件（single source of truth）**：[`anila_plan.md`](./anila_plan.md)。
>
> **Onyx 已搬離本 repo**（2026-04-27）：原本 `onyx/` 是 upstream clone，現由 agent 開發團隊在他們自己的 repo 維護。我方僅保留 handover 文件 [`docs/onyx-target-system-api-spec.md`](./docs/onyx-target-system-api-spec.md) 與 [`docs/onyx-application-plan.md`](./docs/onyx-application-plan.md)。完整變更原因見 [`docs/changelog/2026-04-27-onyx-handover.md`](./docs/changelog/2026-04-27-onyx-handover.md)。需要 Onyx 原始碼請 `git clone` 對方專案。

---

## 整體架構

```mermaid
flowchart TB
    users["🧑‍💻 使用者 / Agent 開發者"]

    subgraph runtime_ui["ANILA Runtime UI :3001"]
        ui_login["登入 / SSO"]
        ui_chat["對話 / 分享 / 交接"]
        ui_dev["Agent Developer Console"]
    end

    subgraph csp["myCSPPlatform :8000"]
        csp_ctrl["Control Plane /api/*<br/>JWT · users · api_keys · models<br/>conversations · shares · handoffs<br/>audit · alerts"]
        csp_data["Data Plane /v1/*<br/>sk- API Key<br/>chat/completions · embeddings"]
    end

    subgraph router["ANILA Router :9000"]
        router_core["router_server<br/>anila-router pseudo-agent<br/>RemoteAgentRegistry"]
    end

    subgraph agents["已註冊 Agent（fork AgenticRAG template）"]
        rag["AgenticRAG :24786<br/>(template)"]
        wf["Workflow Agent"]
        custom["自製 Agent"]
    end

    db[("PostgreSQL<br/>csp schema")]
    llm["落地 LLM / Embedding<br/>(vLLM / Ollama / TGI ...)"]

    users -->|瀏覽器 JWT| runtime_ui
    users -->|OpenAI SDK sk-*| csp_data
    runtime_ui -->|/api/* JWT| csp_ctrl
    runtime_ui -->|/v1/* sk-* · SSE| csp_data
    csp_data -.->|model=anila-router| router
    router -->|GET /v1/agents| csp_data
    router -->|CSP_SERVICE_TOKEN s2s| agents
    agents -->|CSP proxy for LLM / embed| csp_data
    csp_ctrl --> db
    csp_data --> llm
    agents -.CSP proxy.-> llm

    classDef plane fill:#fef3c7,stroke:#d97706
    class csp_ctrl,csp_data plane
```

<details>
<summary>📄 ASCII 版本（離線 / email / 舊 Markdown renderer）</summary>

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

</details>

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
├── AgenticRAG/           # 官方 RAG agent template（完整 framework；開發者 fork 起點）
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
- **Classified 單向閂鎖**：只要 agent `requires_encryption=true` 或 SSE meta 帶 `classified=true`，CSP + Router + UI 三層都把該對話鎖成 classified；**UI 側無任何降級路徑**。並在 UI 持久化時透過 `applyMeta` fire-and-forget 呼叫 `POST /api/conversations/{id}/classify`，重載後 DB 仍保留 classified 旗標。
- **SPA 認證（Wave 2）**：瀏覽器 session 完全走 **httpOnly cookie**（`anila_access_token` / `anila_refresh_token` / 非 httpOnly 的 `anila_csrf`）。SPA 不再持有 API Key — OIDC callback 也不再 mint `sso-*` short-lived key。CSRF 用 **double-submit cookie pattern**，middleware 對 cookie 認證的 POST/PUT/DELETE 檢查 `X-CSRF-Token` header。帶 `Authorization: Bearer` 的 SDK / curl 路徑豁免 CSRF 檢查（非 browser-originated）。
- **雙軌認證**：`/v1/chat/completions` 及其他 `/v1/*` 資料面由 `Caller` dependency 同時接受 JWT（SPA path）與 `sk-*` API Key（SDK path），兩者都歸屬到同一個 `user_id`；僅 API Key 路徑會填 `token_usage.api_key_id`，JWT 路徑落入「Web UI」bucket。
- **API Key 驗證**：建立時後端強制 `name.strip()` 非空 + 至少綁一個 model。OIDC 重複登入會 revoke 該 user 的舊 `sso-*` key。
- **審計日誌**：所有 admin 管理操作（登入 / 登出 / 建立 / 停用 / 改密碼 / 刪除 agent / 刪除 model / health check / encryption toggle 等）自動寫 `audit_logs`，IP 一律從 `X-Forwarded-For` 或 `request.client.host` 填入。
- **使用者最後登入**：`users.last_login_at` 在每次本機 / LDAP / OIDC 登入時更新，admin 可從 UsersView 看到休眠帳號。

---

## 最近更新

### Onyx upstream 移出 monorepo（2026-04-27）

- 原本 `onyx/` 是 4690 個檔（61 MB）的 upstream clone，混在本 repo 已造成 diff/blame/search 雜訊
- agent 開發團隊明確要在他們自家 repo 維護 → 此 repo 不再追 Onyx 程式碼
- 走 `git filter-repo --invert-paths --path onyx/` 從**全 history** 清除（包括所有 branches）
- 結果：`.git` 從 34 MB 縮到 5.7 MB（-83%）、新 clone / fetch 速度顯著改善
- ⚠️ 所有 collaborator 需 `git fetch && git reset --hard origin/<branch>` 同步新 history
- 安全 backup tag：`pre-onyx-filter-repo-2026-04-27`（本地保留 14 天後可刪）
- 完整變更原因 + 操作步驟：[`docs/changelog/2026-04-27-onyx-handover.md`](./docs/changelog/2026-04-27-onyx-handover.md)
- 規格 handover 文件留下：[`docs/onyx-target-system-api-spec.md`](./docs/onyx-target-system-api-spec.md)、[`docs/onyx-application-plan.md`](./docs/onyx-application-plan.md)

### AgenticRAG 升格為官方 RAG Agent Template（2026-04-24）

- 舊的極簡 `anila-rag-sample`（627-line proxy）與獨立 repo `github.com/zzw09773/AgenticRAG`（framework 身份）合併為 **ANILA 平台官方 RAG agent template**
- 完整 framework（65 個 src 模組、23 支測試、tool-driven RAG、Hybrid Search、mxbai cross-encoder reranker、CJK tokenizer、Docling parser、vision pipeline、L1-L3 compact）搬進 monorepo
- 新增 `CspServiceTokenMiddleware` 雙路徑載入（優先 `anila-core` canonical → fallback 本地 in-package copy），保證獨立部署也能跑
- 新增 [`AgenticRAG/anila-agent.yaml`](./AgenticRAG/anila-agent.yaml)（CSP 註冊 manifest）與 [`AgenticRAG/docs/CSP_INTEGRATION.md`](./AgenticRAG/docs/CSP_INTEGRATION.md)（三種註冊方式、s2s auth、trusted user headers、多租戶檢索 patterns）
- `github.com/zzw09773/AgenticRAG` 已歸檔（`isArchived=true`），README 改為 notice 指向本 monorepo
- 對應 commits：`c4bf85a` / `9d5b052` / `59f05f6`

### Auth / Session 重構

- **Wave 1**：`/v1/*` 新增 `Caller` dependency，同時接受 JWT 與 API Key；`token_usage.api_key_id` 改為 `nullable`（migration `0010`），JWT 流量分桶到 dashboard 的 `web_ui_requests`。
- **Wave 2**：SPA 移除 localStorage JWT 與 sessionStorage API Key，完全改走 httpOnly cookie + CSRF；新增 `POST /api/auth/logout`；OIDC callback 不再發 short-lived API Key。
- `users.last_login_at`（migration `0011`）+ audit IP 一致性補齊。

### Agent Console 強化

- `PUT /api/agents/{id}` — owner / admin 可自行編輯 endpoint / description / capabilities / api_version / base_model_id；**刪除仍 admin 限定**，避免孤兒紀錄。
- `base_model_id` **註冊時必填**（原本 optional），並驗證指向 active 的 model_registry row。`AgentResponse` 增加 `base_model_name` / `owner_username` / `capabilities` 欄位。
- `health_status` 在 API 回傳前 normalize（`online` → `healthy`、`offline` → `unhealthy`），統一 dashboard 統計。
- 新增 `POST /api/agents/{id}/health-check` 主動檢查端點。

### UI（ANILA Runtime）

- Chat bubble 重設計：Claude.ai-style flat rounded，Assistant 無頭列框、工具列 hover-reveal、ReasoningSummary 合併 routing trace + thinking 成單行 ghost row。
- Conversation sidebar：title 兩行 clamp 而非截斷；dropdown 改用 `position: fixed` 避開 overflow 切斷；搜尋框 `×` clear button + Esc；tag 搜尋 + 同義詞展開（`特休` 可找到 `年假` / `HR`）。
- Composer：`@` autocomplete 下拉實際可用 agent；paste 時優先 `text/*` 避免文字被 browser fallback 截圖當圖片附件。
- EmptyState 改為單卡「ANILA 可以做什麼？」，prompt 由實際 agent 清單動態產出（不再有假 agent 卡片）。
- Router 系統 prompt 新增「ambiguous → clarify」規則；`_normalize_clarify_bullets` 後處理器把 inline `·` 分隔的候選 agent 轉成 markdown bullet。

### Admin UX 修復

- API Key 建立：前後端 trim 非空 + 至少一 model + `canCreate` disable button。
- Agent 詳情：Owner 顯示 `admin (ID: 1)`；`capabilities` 空時改顯示「尚未設定」。
- Usage chart legend 不再蓋住 x 軸標籤（`containLabel: true` + grid.bottom 留白）。
- Markdown numbered list 密度對齊 OpenWebUI（移除 inline style 與 `white-space: pre-wrap` 繼承）。
- 對話標題 LLM 重複輸出（`AA` pattern）自動 collapse；placeholder 白名單擋 Router fallback 文字汙染 title。

### 測試覆蓋

- 前端 vitest：69 tests（新增 `messageMeta` / `titleClean` / `searchSynonyms`）
- 後端 pytest：50+ tests（新增 `test_cookie_auth` / `test_get_caller` / `test_proxy_jwt_path` / `test_sso_api_key`）

---

## 授權

見 [`LICENSE`](./LICENSE)。Onyx 原 upstream 程式碼已於 2026-04-27 搬離本 repo（詳見上方說明），其原授權由 agent 開發團隊在他們自己的 repo 維護。

---

**Last updated**: 2026-04-24 · **Maintainers**: ANILA 平台團隊 · **Single source of truth**: [`anila_plan.md`](./anila_plan.md)
