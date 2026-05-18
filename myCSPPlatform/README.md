# myCSPPlatform（CSP — Cloud Service Platform）

**ANILA 平台的 Control Plane 與 Data Plane**，權威掌管使用者、API Key、模型註冊、對話、附件、分享、交接、審計、告警。對外同時提供：

- **Control Plane** — `/api/*`（JWT）：管理介面與平台內部溝通
- **Data Plane** — `/v1/*`（`sk-` API Key）：OpenAI 相容代理，讓任何 OpenAI SDK / curl 都能直接當 endpoint 使用

> 上游定位見 repo 根目錄 [`README.md`](../README.md) 與 [`anila_plan.md`](../anila_plan.md)。本 README 聚焦 CSP 自身的部署、配置、API。

---

## 目錄

- [功能總覽](#功能總覽)
- [技術架構](#技術架構)
- [主要資料流程](#主要資料流程)
- [快速開始](#快速開始)
- [配置說明](#配置說明)
- [模型註冊](#模型註冊)
- [API 端點一覽](#api-端點一覽)
- [管理腳本](#管理腳本)
- [專案結構](#專案結構)
- [HTTPS 啟用](#https-啟用)

---

## 功能總覽

### 核心功能
- **API Key 管理** — 發放 `sk-` 前綴 OpenAI 格式金鑰，啟用 / 停用、到期日、模型存取權限、一鍵重新核發
- **模型管理** — LLM / Embedding / VLM / Agent 四種類型；支援啟動時透過環境變數自動註冊；背景定期健康檢查；**Admin 可硬刪除**（當該模型尚有 token_usage 紀錄或被其他模型引用時會 409 拒絕以保留歷史）
- **Agent 開發者流程** — 開發者註冊自己的 agent（endpoint + base_model），由 admin 核准後進入 `/v1/agents` 清單供 Router 分派；**Admin 可硬刪除 agent**
- **Agent 加密標記** — `requires_encryption` 由 admin 在 agent 層面切換；一旦某個 agent 標記為需加密，凡進入該 agent 的對話即被 classified 單向閂鎖
- **OpenAI 相容代理** — `/v1/chat/completions`、`/v1/embeddings`、`/v2/embeddings`，全面支援 SSE Streaming
- **用量追蹤** — Token 用量即時統計、時序圖、依模型 / 使用者 group by、CSV 匯出。24 小時圖有空狀態提示

### 使用者與存取控制
- **自助註冊 + 管理員核准** — 使用者自行申請帳號，未核准前登入顯示「等待核准中」
- **模型授權分級** — admin 指派每位使用者可存取的模型子集；一般使用者無法自選；API Key 建立時自動套用允許清單；allowlist 收縮時既有 API Key 權限同步交集
- **修改密碼即失效舊 Token** — 透過 `token_version` 機制
- **部門管理** — 停用部門時自動解除成員綁定

### 認證整合
- **本機帳號** — JWT（Access Token 15 min + Refresh Token 7 天，httpOnly cookie）
- **OIDC / SSO** — Authorization Code Flow + PKCE (S256) + nonce + JWKS 驗 `id_token`；callback 完全用 cookie，**不再 mint 24h 短效 API Key**（Wave 2 / Sprint 6 X 後）
- **LDAP**（已下線）— Sprint 5 X / migration 0021 移除欄位；對 `/api/auth/login` 帶 `auth_source=ldap` 直接回 400。SSO cutover 路線見 [`../docs/sso-migration.md`](../docs/sso-migration.md)
- **Service-to-service**（Sprint 8 X / Phase A）— Agent / Router / Worker 不再共用單一 env-var token；改走 per-credential `agent_credentials` / `service_clients` table，admin 在 UI issue bootstrap → agent CLI 換 long-lived `csk-` token。詳見 [`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)

### 維運功能
- **告警中心** — 系統異常告警，可 Ack / Resolve
- **審計日誌** — 所有管理操作自動記錄（actor / action / resource_type / resource_id / detail），可多條件篩選
- **平台卡片** — 儀表板可自訂快捷連結（GitLab、n8n、MLSteam 等）

---

## 技術架構

```mermaid
flowchart TB
    user["🧑 使用者 / SDK / Router"]

    subgraph edge["Edge :80"]
        nginx["Nginx (反向代理 + 靜態 SPA)"]
    end

    subgraph app["FastAPI :8000"]
        control["/api/* Control Plane (JWT)<br/>auth · users · api_keys · models<br/>agents · conversations · attachments<br/>shares · handoffs · audit · alerts"]
        data["/v1/* Data Plane (sk- API Key)<br/>chat/completions · embeddings<br/>/v1/agents (給 Router 拉 manifest)"]
    end

    subgraph bg["背景任務"]
        health["health_checker<br/>每 60s poll 模型 /health"]
        usage["usage_writer<br/>批次寫入 token_usage"]
        seed["auto_seed<br/>啟動時註冊 models/agents/links"]
    end

    db[("PostgreSQL :5432<br/>csp schema")]
    models["模型 endpoints<br/>(vLLM / Ollama / Triton)"]
    agents["Agent endpoints<br/>(註冊在 CSP 的 agents)"]

    user -->|瀏覽器 / API| nginx
    nginx --> app
    control --> db
    data --> models
    data -->|"CSP_SERVICE_TOKEN s2s (若 requires_encryption → classified=true)"| agents
    health -.-> models
    health -.-> agents
    usage --> db
    seed -.-> db

    classDef plane fill:#fef3c7,stroke:#d97706
    class control,data plane
```

<details>
<summary>📄 ASCII 版本</summary>

```
                    ┌──────────────┐
      使用者 ──────▶│    Nginx     │ :80 (反向代理 + 靜態 SPA)
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   FastAPI    │ :8000
                    │ (app.main)   │
                    └──┬───────┬───┘
                       │       │
              ┌────────▼──┐ ┌──▼──────────────┐
              │ PostgreSQL │ │  模型 endpoint   │
              │   :5432    │ │  （由 admin 註冊） │
              └────────────┘ └─────────────────┘
```

</details>

| 層級 | 技術 |
|------|------|
| 前端（管理介面） | Vue 3 + Vite + Tailwind CSS + Apache ECharts |
| 後端 | Python 3.11 + FastAPI + SQLAlchemy ORM + Alembic |
| 資料庫 | PostgreSQL 16 |
| 反向代理 | Nginx (Alpine) |
| 容器化 | Docker + Docker Compose |
| 認證 | JWT（Access 15 min + Refresh 7 d）+ LDAP + OIDC |

---

## 主要資料流程

### 1. Chat Completion（OpenAI 相容代理）

```
┌──────────┐ POST /v1/chat/completions     ┌──────────────────────┐
│  Client  │  Authorization: Bearer sk-... │ api_key_auth         │
│  (OpenAI │──────────────────────────────▶│ middleware           │
│   SDK)   │                               │  - 驗 sk- key 是否啟用 │
└──────────┘                               │  - 驗 model 在        │
     ▲                                     │    allowed_models     │
     │  SSE / JSON                         └──────────┬───────────┘
     │                                                │
     │                                                ▼
     │                                     ┌──────────────────────┐
     │                                     │ proxy_service        │
     │                                     │  - 依 model_type 路由 │
     │                                     │    llm / vlm / agent  │
     │                                     │  - 若 agent 需加密     │
     │                                     │    → classified=true  │
     │                                     │  - SSE 逐 chunk forward│
     │                                     └──────────┬───────────┘
     │                                                │
     │                                                ▼
     │                                     ┌──────────────────────┐
     │                                     │ model endpoint       │
     │                                     │ (vLLM / Ollama /     │
     │                                     │  Triton / agent)     │
     │                                     └──────────┬───────────┘
     │                                                │
     │          ┌───────────────┐                     │
     └──────────│ usage_writer  │◀────────────────────┘
                │ (批次寫入)     │   token 用量事件
                └───────┬───────┘
                        ▼
                  ┌──────────┐
                  │token_    │
                  │usage     │
                  └──────────┘
```

### 2. 使用者註冊 → 核准 → 取得 API Key

```
使用者 ── POST /api/auth/register ──▶ user 寫入 DB（is_approved=false）
                                     ↓ 登入回「等待核准」

admin ── POST /api/users/{id}/approve ──▶ is_approved=true
                                         audit_log 記一筆

使用者 ── POST /api/auth/login ──▶ JWT（access + refresh）
                                 ↓
使用者 ── POST /api/keys ──▶ 回傳一次性的 sk-xxxxx（只能看這一次）
                             ↓  自動用該 user 的 allowed_models 建 permission
                             ↓
         → 放進 OpenAI SDK 當 API Key 用
```

### 3. Agent 註冊 → 核准 → 上線

```
developer 於 UI「Developer」分頁 ── POST /api/agents (register)
                                   ↓  endpoint_url / base_model / requires_encryption
                                   ↓  status=pending，audit_log

admin ── POST /api/agents/{id}/approve ──▶ status=approved
                                          is_active=true
                                          （出現在 /v1/agents 清單）

Router /v1/agents 輪詢 ──▶ RemoteAgentRegistry cache
                          ↓
UI 呼叫 anila-router pseudo-agent ──▶ Router 分派 ──▶ agent endpoint
                                                      ↓
                                                  （若 requires_encryption）
                                                      ↓
                                              SSE meta: classified=true
                                                      ↓
                                              UI 對話單向 latch 成 classified
```

---

## 快速開始

### 前置需求
- Docker 及 Docker Compose v2+
- Git

### 步驟

```bash
# 1. 取得程式碼
git clone <repo-url> && cd myCSPPlatform

# 2. 建立環境變數檔
cp .env.example .env

# 3. 編輯配置（至少修改 SECRET_KEY 和 ADMIN_PASSWORD）
vim .env

# 4. 啟動平台
./start.sh up
```

啟動後存取：

| 服務 | 網址 |
|------|------|
| 管理平台 | http://localhost |
| API 文件 (Swagger) | http://localhost/docs |
| 健康檢查 | http://localhost/health |

預設管理員：`admin` / `changeme`（**務必於 `.env` 修改 `ADMIN_PASSWORD`**）

> **整合跑法**：如果你是要跑整個 ANILA stack（CSP + Router + UI），請用 **repo 根目錄**的 `docker-compose.yml`，不要用 `myCSPPlatform/docker/docker-compose.yml`。

---

## 配置說明

所有配置透過 `.env` 檔案管理。

### 基本設定

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `SECRET_KEY` | `your-secret-key-...` | JWT 簽署密鑰，**務必修改** |
| `DEBUG` | `false` | 除錯模式（啟用 SQL echo） |

### 資料庫

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql://csp:csp_password@postgres:5432/csp` | PostgreSQL 連線字串 |
| `DB_USER` | `csp` | PostgreSQL 使用者 |
| `DB_PASSWORD` | `csp_password` | **務必修改** |
| `DB_NAME` | `csp` | 資料庫名稱 |

### JWT 認證

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access Token 有效時間（分鐘） |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh Token 有效時間（天） |

### 管理員帳號

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `ADMIN_USERNAME` | `admin` | 首次啟動自動建立 |
| `ADMIN_PASSWORD` | `changeme` | **務必修改** |

### Proxy 設定

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `EMBEDDING_TIMEOUT` | `30` | Embedding 請求逾時（秒） |
| `LLM_TIMEOUT` | `120` | LLM 請求逾時（秒） |
| `PROXY_MAX_RETRIES` | `3` | 代理失敗重試次數 |
| `PROXY_RETRY_BASE_DELAY` | `0.5` | 重試基礎延遲（秒，指數退避） |

### 背景任務

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `HEALTH_CHECK_INTERVAL` | `60` | 健康檢查間隔（秒） |
| `USAGE_BATCH_SIZE` | `100` | 用量批次寫入筆數 |
| `USAGE_FLUSH_INTERVAL` | `5` | 用量批次寫入間隔（秒） |

### Nginx

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `NGINX_PORT` | `80` | Nginx 對外 port |
| `SITE_URL` | `http://localhost` | 瀏覽器存取的基底 URL（平台卡片連結用） |

---

## 模型註冊

模型可在啟動時透過環境變數自動註冊，兩種方式可並存。

### 方式一：JSON 陣列

適合一次配置多個模型：

```env
AUTO_REGISTER_MODELS=[
  {
    "name": "llama3-70b",
    "display_name": "Llama 3 70B Instruct",
    "model_type": "llm",
    "endpoint_url": "http://vllm-llm:8000",
    "api_version": "v1",
    "description": "vLLM 部署的 Llama 3 70B",
    "context_window": 8192
  },
  {
    "name": "nv-embed-v2",
    "display_name": "NVIDIA NV-Embed V2",
    "model_type": "embedding",
    "endpoint_url": "http://triton-embedding:8000",
    "api_version": "v2"
  },
  {
    "name": "aia/asrd",
    "display_name": "AIA ASRD Agent",
    "model_type": "agent",
    "endpoint_url": "http://mlsteam-host:45023",
    "api_version": "v1",
    "base_model": "llama3-70b"
  }
]
```

**模型類型 (`model_type`)：** `llm`、`embedding`、`vlm`、`agent`
**Agent 模型：** 透過 `base_model` 指定底層模型的 `name`，系統自動建立關聯。

### 方式二：獨立環境變數

格式：`MODEL_<NAME>_<FIELD>`。`NAME` 以底線分隔，自動轉為小寫連字號（`LLAMA3_70B` → `llama3-70b`）。

```env
MODEL_LLAMA3_70B_HOST=vllm-llm
MODEL_LLAMA3_70B_PORT=8000
MODEL_LLAMA3_70B_TYPE=llm
MODEL_LLAMA3_70B_DISPLAY_NAME=Llama 3 70B Instruct
MODEL_LLAMA3_70B_CONTEXT_WINDOW=8192
```

**可用 FIELD：** `HOST`、`PORT`、`TYPE`、`DISPLAY_NAME`、`API_VERSION`、`DESCRIPTION`、`CONTEXT_WINDOW`、`BASE_MODEL`

> 若 JSON 中已有相同 `name`，env var 不會覆蓋。

### 平台卡片連結

```env
AUTO_REGISTER_LINKS=[
  {"name": "n8n 工作流程", "url": "http://n8n:5678", "icon": "workflow", "description": "自動化工作流程平台"},
  {"name": "MLSteam", "url": "https://mlsteam.example.com", "icon": "cpu", "description": "MLOps 平台"}
]
```

---

## API 端點一覽

### 認證 (`/api/auth`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/auth/register` | 自助申請帳號（需管理員核准） |
| POST | `/api/auth/login` | 本機 / LDAP 登入，取得 access + refresh token |
| POST | `/api/auth/refresh` | 使用 refresh token 換發新 token |
| POST | `/api/auth/logout` | 登出 — 清 httpOnly cookie + bump token_version 撤銷既存 access token |
| GET | `/api/auth/me` | 取得當前使用者資訊 |
| PUT | `/api/auth/password` | 修改自身密碼（舊 Token 同步作廢） |
| GET | `/api/auth/providers` | 列出可用的公開 OIDC Provider |
| GET | `/api/auth/oidc/{id}/start` | 取得 OIDC 授權跳轉 URL（含 PKCE state + nonce） |
| GET | `/api/auth/oidc/{id}/callback` | OIDC Callback（瀏覽器重導，cookie-only；Wave 2 起不再回帶 API Key） |

### 使用者管理 (`/api/users`) — 需 Admin

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/users` | 列出所有使用者 |
| POST | `/api/users` | 建立使用者 |
| GET | `/api/users/{id}` | 取得使用者詳情 |
| PUT | `/api/users/{id}` | 更新使用者（admin→user 時自動 cascade API Key 權限） |
| POST | `/api/users/{id}/reset-password` | 重設密碼（舊 Token 失效） |
| POST | `/api/users/{id}/approve` | 核准自助申請 |
| DELETE | `/api/users/{id}` | 停用使用者（舊 Token 立即失效） |
| GET | `/api/users/me/allowed-models` | 取得自身可用模型 |
| GET | `/api/users/{id}/allowed-models` | 取得指定使用者可用模型 |
| PUT | `/api/users/{id}/allowed-models` | 設定使用者可用模型，並 cascade 至 API Key |

### API Key 管理 (`/api/keys`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/keys` | 列出 API Keys（admin 看全部，一般看自己的） |
| POST | `/api/keys` | 建立新 Key（回傳一次性的 `sk-...`） |
| GET | `/api/keys/{id}` | 取得 Key 詳情 |
| PUT | `/api/keys/{id}` | 更新 Key 設定（改權限需 Admin） |
| POST | `/api/keys/{id}/regenerate` | 重新核發（撤銷舊 Key，複製名稱/權限/到期日建新 Key） |
| DELETE | `/api/keys/{id}` | 撤銷 Key |

### 模型管理 (`/api/models`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/models` | 列出模型（一般使用者僅看 allowlist 內） |
| POST | `/api/models` | 註冊新模型（需 Admin） |
| GET | `/api/models/{id}` | 取得模型詳情 |
| PUT | `/api/models/{id}` | 更新（需 Admin） |
| DELETE | `/api/models/{id}` | **停用**模型（軟刪除，需 Admin） |
| DELETE | `/api/models/{id}/purge` | **硬刪除**模型（需 Admin；若有 token_usage 或被其他模型引用則 409） |
| POST | `/api/models/{id}/health-check` | 手動觸發健康檢查（需 Admin） |

### Agent 管理 (`/api/agents`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/agents` | 列出 agent（開發者看自己的 + approved，admin 看全部） |
| POST | `/api/agents` | Developer 註冊 agent（status=pending） |
| POST | `/api/agents/{id}/approve` | 核准（需 Admin） |
| POST | `/api/agents/{id}/reject` | 拒絕（需 Admin） |
| PUT | `/api/agents/{id}/encryption` | 切換 `requires_encryption`（需 Admin） |
| DELETE | `/api/agents/{id}` | **硬刪除** agent（需 Admin） |

#### Sprint 8 X / Phase A — service-token bootstrap

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/agents/{id}/issue-bootstrap` | 核發單次使用 `bsk-` token（需 Admin），15 分鐘預設 TTL |
| POST | `/api/agents/{id}/bootstrap` | 公開（token-gated）— agent 用 `bsk-` 換長效 `csk-` |
| POST | `/api/agents/{id}/credentials/issue-static` | 核發長效 `csk-`，跳過 bootstrap（Phase F Tier 0；需 Admin） |
| GET | `/api/agents/{id}/credentials` | 列出該 agent 的 active + revoked credentials（需 Admin） |
| POST | `/api/agents/{id}/credentials/{cid}/rotate` | 輪替 credential，舊 token 24h grace 仍可用（需 Admin） |
| DELETE | `/api/agents/{id}/credentials/{cid}` | 立即吊銷 credential（需 Admin） |
| GET | `/api/agents/{id}/credentials/me` | Agent self-introspection（auth = service token；Phase F Tier 1） |

#### Sprint 13 — agent runtime hot-reload + resume proxy

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/agents/{id}/runtime-config` | 讀 agent 的 admin-set runtime knobs（owner / admin） |
| PATCH | `/api/agents/{id}/runtime-config` | 設或清空 runtime config；body `null` = 清除（用 code defaults），`{}` = 顯式空（清空 lists / 無 guardrails）。Audit-logged |
| GET | `/api/agents/me/runtime-config` | Agent 自取（auth = `X-CSP-Service-Token`）。回 `{runtime_config, etag}`，agent 端 30s 輪詢 ETag-cached |
| POST | `/v1/agents/{name}/sessions/{sid}/answer` | Resume proxy — 把 `{interrupt_id, answer}` 透過 identity-injection + per-agent service-token swap forward 到 agent 的 `/sessions/{id}/answer`（資料面，bearer auth） |

### Service Clients (`/api/service-clients`) — 需 Admin

Router / worker / admin-tool 用的 service token 管理（不是 agent，但走相同的 envelope + lookup hash 機制）。

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/service-clients` | 列出全部 |
| POST | `/api/service-clients` | 建立新 client + 核發初始 token |
| POST | `/api/service-clients/{id}/issue-static` | 直接重發 token（無 grace） |
| POST | `/api/service-clients/{id}/rotate` | 輪替 token（含 grace） |
| DELETE | `/api/service-clients/{id}` | 吊銷 |

### 對話 / 附件 / 分享 / 交接（UI 用）

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET / POST | `/api/conversations` | 列出 / 建立對話 |
| GET / PUT / DELETE | `/api/conversations/{id}` | CRUD |
| POST | `/api/conversations/{id}/messages` | 寫入一則訊息（UI 在 SSE 完成後呼叫） |
| GET / POST / DELETE | `/api/conversations/{id}/shares` | 公開分享連結 |
| POST | `/api/attachments` | Multipart 上傳（回傳 attachment_id） |
| POST | `/api/handoffs` | 建立 handoff |
| POST | `/api/handoffs/{id}/{accept\|reject\|cancel}` | 處理 handoff |

### 部門管理 (`/api/departments`) — 需 Admin
`GET` / `POST` / `PUT` / `DELETE /api/departments[/{id}]`

### Auth Provider (`/api/auth-providers`) — 需 Admin
`GET` / `POST` / `PUT` / `DELETE /api/auth-providers[/{id}]`

### 告警中心 (`/api/alerts`) — 需 Admin

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/alerts` | 列出（可 status / severity / category 篩選） |
| GET | `/api/alerts/summary` | 告警統計 |
| POST | `/api/alerts/{id}/ack` | 確認 |
| POST | `/api/alerts/{id}/resolve` | 手動解除 |

### 審計日誌 (`/api/audit-logs`) — 需 Admin
`GET /api/audit-logs`（可 action / resource_type / actor / status 篩選，最多 500 筆）

### 用量統計 (`/api/usage`)

`GET /api/usage/{summary,chart,top-models,top-users,top-departments,export}`

Sprint 8 X / Phase G — caller attribution 計量（admin only）：

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/usage/top-agents?days=30&limit=10` | 過去 N 天 token 消耗最多的 agent |
| GET | `/api/usage/by-base-model?days=30` | group by `agents.base_model_id`，看哪些底層 LLM 被 agent 透過 CSP 用了多少 |
| GET | `/api/usage/by-client?days=30` | group by `service_clients.id`（Router / worker） |
| GET | `/api/usage/agents/{id}?days=30` | 單一 agent 的時序 + summary |

### 平台連結 (`/api/platform-links`)
`GET / POST / PUT / DELETE /api/platform-links[/{id}]`

### OpenAI 相容代理（Data Plane）

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/v1/chat/completions` | Chat（LLM / VLM / Agent，SSE 串流） |
| POST | `/v1/embeddings` | Embeddings v1 |
| POST | `/v2/embeddings` | Embeddings v2（Triton 等） |
| GET | `/v1/agents` | 供 Router 撈的 agent manifest（含 `requires_encryption`） |

**代理使用方式：**

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3-70b",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

### 其他
- `GET /health` — 容器編排健康檢查
- `GET /docs` — Swagger UI

---

## 管理腳本

```bash
./start.sh up        # 啟動所有服務（含建構映像）
./start.sh down      # 停止所有服務
./start.sh restart   # 重啟
./start.sh logs      # 查看所有日誌（Ctrl+C 退出）
./start.sh logs csp  # 只看 CSP 後端日誌
./start.sh status    # 查看容器狀態
./start.sh build     # 重新建構（不啟動）
./start.sh shell     # 進入後端容器
```

---

## 專案結構

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── auth.py              # 登入、註冊、OIDC callback、密碼
│   │   │   ├── users.py             # 使用者 CRUD + allowlist + 核准
│   │   │   ├── api_keys.py          # API Key CRUD + regenerate
│   │   │   ├── models.py            # 模型管理 + 健康檢查 + purge
│   │   │   ├── agents.py            # Agent 註冊 / 核准 / 加密 / 刪除
│   │   │   ├── conversations.py     # UI 對話 CRUD
│   │   │   ├── attachments.py       # multipart 附件
│   │   │   ├── public_share.py      # 公開分享連結
│   │   │   ├── handoffs.py          # Agent 交接
│   │   │   ├── departments.py
│   │   │   ├── auth_providers.py    # LDAP / OIDC Provider
│   │   │   ├── alerts.py
│   │   │   ├── audit_logs.py
│   │   │   ├── usage.py
│   │   │   ├── platform_links.py
│   │   │   └── proxy.py             # /v1/* OpenAI 相容代理
│   │   ├── models/                  # SQLAlchemy ORM
│   │   ├── schemas/                 # Pydantic schema
│   │   ├── services/
│   │   │   ├── auth_service.py      # JWT 驗證、token_version
│   │   │   ├── proxy_service.py     # 代理核心（含 classified latch）
│   │   │   ├── health_checker.py    # 背景健康檢查
│   │   │   ├── usage_writer.py      # 批次寫入用量
│   │   │   ├── audit_service.py
│   │   │   ├── alert_service.py
│   │   │   ├── auto_seed.py         # 啟動自動建立 admin / 模型
│   │   │   └── startup_migrations.py
│   │   ├── middleware/api_key_auth.py
│   │   ├── config.py
│   │   ├── database.py
│   │   └── main.py
│   ├── migrations/                  # Alembic migrations
│   │   └── versions/
│   │       ├── 0006_drop_quota_rate_limit.py
│   │       └── 0007_drop_model_requires_encryption.py
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── views/                   # DashboardView, ApiKeysView, ModelsView,
│   │   │                            # UsageView, UsersView, DeveloperAgentsView, ...
│   │   ├── components/
│   │   │   ├── layout/              # AppHeader、AppSidebar、AppLayout
│   │   │   ├── charts/              # UsageLineChart（含空狀態）
│   │   │   ├── common/              # ConfirmDialog
│   │   │   └── dashboard/           # PlatformCard、UsageSummaryCard
│   │   ├── api/                     # Axios client
│   │   ├── stores/                  # Pinia stores
│   │   └── router/
│   └── package.json
├── docker/
│   ├── Dockerfile                   # Multi-stage（Node + Python）
│   ├── docker-compose.yml           # 僅 CSP 單獨開發用（整合部署請用 repo 根目錄）
│   └── nginx.conf
├── scripts/
├── start.sh
├── .env.example
└── README.md
```

---

## HTTPS 啟用

1. 將憑證放入 `docker/certs/`（`server.crt` + `server.key`）
2. 編輯 `docker/docker-compose.yml`，取消 SSL port 與 certs volume 的註解
3. 編輯 `docker/nginx.conf`，取消 HTTPS server block 的註解
4. `./start.sh restart`

---

## 授權

見 repo 根 [`LICENSE`](../LICENSE)。內部使用專案。

---

## Release Notes

### 2026-05-03 — Sprint 13: agent runtime hot-reload + resume proxy

- **Migration `0029`** — `agents` 表新增 `runtime_config` JSONB 欄位（nullable）。Admin 可放任意 JSON 形狀的「per-agent runtime knobs」（tool permissions / workspace caps / guardrails），agent process 每 30 秒 ETag-cached 輪詢自取並套用，無需重啟。
- **3 個新 API endpoint**：`GET` / `PATCH /api/agents/{id}/runtime-config`（owner / admin）、`GET /api/agents/me/runtime-config`（agent 用 `X-CSP-Service-Token` 自取）。語意：`null` = 清除 override（用 code defaults）、`{}` = 顯式空。Audit-logged。
- **新 resume proxy** `POST /v1/agents/{name}/sessions/{sid}/answer`：把使用者面 UI 的 `{interrupt_id, answer}` 透過 `_build_downstream_headers`（同 `chat_completions` 的 identity injection + per-agent service-token swap）forward 到 agent 的 `/sessions/{id}/answer`。SSE pass-through。讓 Sprint 9 的 ask_user / plan / tool_approval interrupts 可以從 user UI 走 Router → CSP → agent 完整鏈路 resume。
- **新 admin view** `AgentRuntimeConfigView.vue`：三段式編輯器（permissions / workspace / guardrails），帶 raw JSON preview 與 clear-override 按鈕。從 `DeveloperAgentsView` 的 detail drawer 入口進。
- **DeveloperGuideView 大幅擴充**：5 個新中文章節（Sprint 9 agentic loop primitives / Sprint 11 per-tool ASK / Sprint 12 workspace + guardrails / Sprint 13 runtime_config 用法）。
- 對應 anila-core **v0.12.0**。完整 changelog：[`../anila-core/CHANGELOG.md`](../anila-core/CHANGELOG.md)。

### 2026-04-24 — AgenticRAG template 同步

- `AUTO_REGISTER_AGENTS` 可直接消費 [`AgenticRAG/anila-agent.yaml`](../AgenticRAG/anila-agent.yaml)（YAML → JSON one-liner，見該檔註解）
- Agent 註冊流程文件化：見 [`AgenticRAG/docs/CSP_INTEGRATION.md`](../AgenticRAG/docs/CSP_INTEGRATION.md)（三種註冊方式、s2s auth、trusted user headers 細節）

### Wave 2 — SPA 認證重構（2026-03）

- 瀏覽器 session 改走 **httpOnly cookie**（`anila_access_token` / `anila_refresh_token` / `anila_csrf`）；SPA 不再持有 API Key
- OIDC callback **不再** mint `sso-*` short-lived key（之前會發 24h 短效 key 給 UI，已廢）
- CSRF 用 **double-submit cookie pattern**；帶 `Authorization: Bearer` 的 SDK path 豁免 CSRF
- `POST /api/auth/logout` 新增
- Migration `0011`：`users.last_login_at` + audit IP 一致性

### Wave 1 — 雙軌認證（2026-02）

- `/v1/*` 資料面新增 `Caller` dependency，同時接受 JWT（SPA path）與 `sk-*` API Key（SDK path）
- Migration `0010`：`token_usage.api_key_id` 改為 nullable；JWT 流量分桶到 dashboard 的 `web_ui_requests`

### Wave 0 — Quota 子系統移除

- Migration `0006_drop_quota_rate_limit`：落地 on-prem 部署不需要 token quota，整個子系統移除
- Migration `0007_drop_model_requires_encryption`：加密改為 agent-level 屬性，不在 model-level 設定

### Agent Console 強化

- `PUT /api/agents/{id}` — owner / admin 可自行編輯 endpoint / description / capabilities；**刪除仍 admin 限定**
- `base_model_id` **註冊時必填**；`AgentResponse` 增 `base_model_name` / `owner_username` / `capabilities`
- `POST /api/agents/{id}/health-check` 主動檢查
- `health_status` normalize：`online` → `healthy`、`offline` → `unhealthy`

---

## 相關文件

- 平台整體：[`../README.md`](../README.md)
- Runtime foundation：[`../anila-core/README.md`](../anila-core/README.md)
- Router：[`../anila-core-router/README.md`](../anila-core-router/README.md)
- **RAG agent template**：[`../AgenticRAG/README.md`](../AgenticRAG/README.md) · [`../AgenticRAG/docs/CSP_INTEGRATION.md`](../AgenticRAG/docs/CSP_INTEGRATION.md)
- UI：[`../ANILA_UI/anila-ui/README.md`](../ANILA_UI/anila-ui/README.md)
- 路線圖：[`../anila_plan.md`](../anila_plan.md)

---

**Last updated**: 2026-05-03 (Sprint 13 — runtime_config + resume proxy) · **Role**: Control + Data Plane · **Authoritative for**: users · api_keys · models · agents · agent_credentials · agents.runtime_config · service_clients · token_usage · audit_logs

## Generated illustrations (FLUX integration, Phase 6)

Slides can request a freshly generated illustration via FLUX.2-dev when
the knowledge base has no suitable existing image. The LLM may set
`Slide.image_prompt` (English, 50–500 chars) on any slide that needs
a visual. The CSP `_hydrate_images` step calls `flux2-dev` directly
via `FluxImageProvider`, caches by SHA256(prompt + aspect_ratio), and
inlines the resulting PNG as a base64 data URL in `image_data` —
which is exactly the same shape that pre-existing `image_ref` produces,
so the pptx-renderer needs no changes.

**Env vars (CSP service):**
- `FLUX_BACKEND_URL` (required to enable; e.g. `http://flux2-dev:8000`)
- `FLUX_CACHE_DIR` (default: `$INGESTION_UPLOAD_DIR/flux-cache`)
- `FLUX_MAX_CONCURRENT` (default: `4`)
- `FLUX_TIMEOUT_SECONDS` (default: `180`)

**Failure handling:** any FLUX backend error drops the prompt silently
and the renderer falls back to standard layout — identical behavior to
a missing `image_ref`.

**Cache invalidation:** none. Prompts are content-addressed; to drop
a cached image, delete the file under `$FLUX_CACHE_DIR`.
