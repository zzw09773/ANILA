# CSP Platform

**Cloud Service Platform** — 企業內部 AI 模型服務的統一管理平台。

CSP 為組織提供一個集中式介面，用於管理多種 AI 模型（LLM、Embedding、VLM、Agent）的存取權限、API Key 發放、用量追蹤與健康監控。所有模型服務透過 OpenAI 相容 API 對外提供，讓使用者無需修改現有工具即可串接。

---

## 功能總覽

### 核心功能
- **API Key 管理** — 發放 `sk-` 前綴的 OpenAI 格式金鑰，支援啟用 / 停用、到期日設定、模型存取權限控制、一鍵重新核發（Regenerate）
- **模型管理** — 支援 LLM、Embedding、VLM、Agent 四種類型，可透過環境變數自動註冊；背景定期健康檢查
- **OpenAI 相容代理** — `/v1/chat/completions`、`/v1/embeddings`、`/v2/embeddings`，支援 SSE Streaming
- **用量追蹤** — Token 用量即時統計、時序圖表、依模型 / 使用者分組、CSV 匯出

### 使用者與存取控制
- **使用者管理** — 建立 / 編輯 / 停用使用者、管理員重設密碼
- **自助註冊** — 使用者可自行申請帳號（需管理員核准），未核准前登入顯示「等待核准中」
- **修改密碼** — 使用者可在已登入狀態修改自己的密碼，變更後舊 Token 立即失效
- **模型授權分級** — 管理員可指派每位使用者可存取的模型子集；一般使用者無法自選模型，API Key 建立時自動套用允許清單；allowlist 收縮時既有 API Key 權限同步交集
- **部門管理** — 將使用者歸屬於部門；停用部門時自動解除成員綁定

### 認證整合
- **本機帳號** — JWT（Access Token 15 分鐘 + Refresh Token 7 天），Token 版本機制（`token_version`）確保密碼變更 / 帳號停用後舊 Token 立即失效
- **LDAP 登入** — 透過 Auth Provider 設定連線 DN、Filter、StartTLS 等
- **OIDC / SSO 登入** — Authorization Code Flow，支援自訂 Issuer、Scopes、Claim 映射

### 維運功能
- **告警中心** — 系統異常告警，可標記確認（Acknowledge）或手動解除（Resolve）
- **審計日誌** — 所有管理操作（建立 / 更新 / 停用 / 登入 / 密碼變更）自動記錄，可依動作、資源類型、操作者篩選
- **平台卡片** — 儀表板顯示可自訂的快捷連結（GitLab、n8n、MLSteam 等）

---

## 技術架構

```
                    ┌──────────────┐
      使用者 ──────▶│    Nginx     │ :80 (反向代理 + Rate Limit)
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   FastAPI    │ :8000 (後端 + SPA)
                    │  (CSP Core)  │
                    └──┬───────┬───┘
                       │       │
              ┌────────▼──┐ ┌──▼──────────┐
              │ PostgreSQL │ │  模型服務    │
              │   :5432    │ │ vLLM/Triton │
              └────────────┘ │ MLSteam/... │
                             └─────────────┘
```

| 層級 | 技術 |
|------|------|
| 前端 | Vue 3 + Vite + Tailwind CSS + Apache ECharts |
| 後端 | Python 3.11 + FastAPI + SQLAlchemy ORM |
| 資料庫 | PostgreSQL 16 |
| 反向代理 | Nginx (Alpine) |
| 容器化 | Docker + Docker Compose |
| 認證 | JWT (Access Token 15min + Refresh Token 7d) + LDAP + OIDC |

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

預設管理員帳號：`admin` / `changeme`（請於 `.env` 修改 `ADMIN_PASSWORD`）

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
| `DB_USER` | `csp` | PostgreSQL 使用者（docker-compose 使用） |
| `DB_PASSWORD` | `csp_password` | PostgreSQL 密碼，**務必修改** |
| `DB_NAME` | `csp` | 資料庫名稱 |

### JWT 認證

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access Token 有效時間（分鐘） |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh Token 有效時間（天） |

### 管理員帳號

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `ADMIN_USERNAME` | `admin` | 首次啟動自動建立的管理員帳號 |
| `ADMIN_PASSWORD` | `changeme` | 管理員初始密碼，**務必修改** |

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
| `SITE_URL` | `http://localhost` | 瀏覽器存取的基底 URL，用於平台卡片連結 |

---

## 模型註冊

模型可在啟動時透過環境變數自動註冊。支援兩種方式，可並存使用。

### 方式一：JSON 陣列

適合一次配置多個模型，在 `.env` 中設定 `AUTO_REGISTER_MODELS`：

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

**Agent 模型：** 透過 `base_model` 欄位指定底層模型的 `name`，系統自動建立關聯。

### 方式二：獨立環境變數

適合逐一配置，格式為 `MODEL_<NAME>_<FIELD>`。`NAME` 使用底線分隔，系統自動轉為小寫連字號（如 `LLAMA3_70B` → `llama3-70b`）。

```env
MODEL_LLAMA3_70B_HOST=vllm-llm
MODEL_LLAMA3_70B_PORT=8000
MODEL_LLAMA3_70B_TYPE=llm
MODEL_LLAMA3_70B_DISPLAY_NAME=Llama 3 70B Instruct
MODEL_LLAMA3_70B_CONTEXT_WINDOW=8192

MODEL_NV_EMBED_V2_HOST=triton-embedding
MODEL_NV_EMBED_V2_PORT=8000
MODEL_NV_EMBED_V2_TYPE=embedding
MODEL_NV_EMBED_V2_API_VERSION=v2
```

**可用 FIELD：** `HOST`、`PORT`、`TYPE`、`DISPLAY_NAME`、`API_VERSION`、`DESCRIPTION`、`CONTEXT_WINDOW`、`BASE_MODEL`

> 兩種方式可並存。若 JSON 中已有相同 `name` 的模型，env var 不會覆蓋。

### 平台卡片連結

在 `.env` 中設定 `AUTO_REGISTER_LINKS`：

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
| GET | `/api/auth/me` | 取得當前使用者資訊 |
| PUT | `/api/auth/password` | 修改自身密碼（Token 同步作廢） |
| GET | `/api/auth/providers` | 列出可用的公開 SSO/LDAP Provider |
| GET | `/api/auth/oidc/{id}/start` | 取得 OIDC 授權跳轉 URL |
| GET | `/api/auth/oidc/{id}/callback` | OIDC Callback（瀏覽器重導用） |

### 使用者管理 (`/api/users`) — 需 Admin 權限

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/users` | 列出所有使用者 |
| POST | `/api/users` | 建立使用者 |
| GET | `/api/users/{id}` | 取得使用者詳情 |
| PUT | `/api/users/{id}` | 更新使用者（角色 admin→user 時自動 cascade API Key 權限） |
| POST | `/api/users/{id}/reset-password` | 重設使用者密碼（舊 Token 失效） |
| POST | `/api/users/{id}/approve` | 核准自助申請的使用者帳號 |
| DELETE | `/api/users/{id}` | 停用使用者（舊 Token 立即失效） |
| GET | `/api/users/me/allowed-models` | 取得自身可用模型清單 |
| GET | `/api/users/{id}/allowed-models` | 取得指定使用者的可用模型 |
| PUT | `/api/users/{id}/allowed-models` | 設定使用者可用模型，並 cascade 至其 API Key |

### API Key 管理 (`/api/keys`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/keys` | 列出 API Keys（admin 看全部，一般使用者看自己的） |
| POST | `/api/keys` | 建立新 API Key（回傳唯一一次 `sk-` 格式金鑰） |
| GET | `/api/keys/{id}` | 取得 Key 詳情 |
| PUT | `/api/keys/{id}` | 更新 Key 設定（更新模型權限需 Admin） |
| POST | `/api/keys/{id}/regenerate` | 重新核發（撤銷舊 Key，複製名稱/權限/到期日建新 Key） |
| DELETE | `/api/keys/{id}` | 撤銷 Key |

### 模型管理 (`/api/models`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/models` | 列出模型（一般使用者只看 allowlist 內的） |
| POST | `/api/models` | 註冊新模型（需 Admin） |
| GET | `/api/models/{id}` | 取得模型詳情（一般使用者需在 allowlist 內） |
| PUT | `/api/models/{id}` | 更新模型設定（需 Admin） |
| DELETE | `/api/models/{id}` | 停用模型（需 Admin） |
| POST | `/api/models/{id}/health-check` | 手動觸發健康檢查（需 Admin） |

### 部門管理 (`/api/departments`) — 需 Admin 權限

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/departments` | 列出所有部門（含使用者統計） |
| POST | `/api/departments` | 建立部門 |
| PUT | `/api/departments/{id}` | 更新部門 |
| DELETE | `/api/departments/{id}` | 停用部門（成員自動解除綁定） |

### SSO / LDAP / OIDC Provider (`/api/auth-providers`) — 需 Admin 權限

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/auth-providers` | 列出所有 Auth Provider |
| POST | `/api/auth-providers` | 建立 Provider（ldap / oidc） |
| PUT | `/api/auth-providers/{id}` | 更新 Provider |
| DELETE | `/api/auth-providers/{id}` | 停用 Provider |

### 告警中心 (`/api/alerts`) — 需 Admin 權限

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/alerts` | 列出告警（可依 status / severity / category 篩選） |
| GET | `/api/alerts/summary` | 告警統計摘要 |
| POST | `/api/alerts/{id}/ack` | 確認告警 |
| POST | `/api/alerts/{id}/resolve` | 手動解除告警 |

### 審計日誌 (`/api/audit-logs`) — 需 Admin 權限

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/audit-logs` | 查詢審計日誌（可依 action / resource_type / actor / status 篩選，最多 500 筆） |

### 用量統計 (`/api/usage`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/usage/summary` | 過去 24 小時用量摘要 |
| GET | `/api/usage/chart` | 時序圖表資料（支援 group_by） |
| GET | `/api/usage/top-models` | Top N 模型排行（30 天） |
| GET | `/api/usage/top-users` | Top N 使用者排行（30 天） |
| GET | `/api/usage/export` | 匯出用量 CSV |

### 平台連結 (`/api/platform-links`)

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/api/platform-links` | 列出所有平台卡片 |
| POST | `/api/platform-links` | 新增平台卡片（需 Admin） |
| PUT | `/api/platform-links/{id}` | 更新卡片（需 Admin） |
| DELETE | `/api/platform-links/{id}` | 刪除卡片（需 Admin） |

### OpenAI 相容代理

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/v1/chat/completions` | Chat Completions（LLM / VLM / Agent，支援 Streaming） |
| POST | `/v1/embeddings` | Embeddings (v1 格式) |
| POST | `/v2/embeddings` | Embeddings (v2 格式，Triton 等) |

**代理使用方式：** 以 API Key (`sk-...`) 作為 Bearer Token，直接當 OpenAI endpoint 使用。

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

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/health` | 健康檢查（容器編排用） |
| GET | `/docs` | Swagger UI API 文件 |

---

## 管理腳本

```bash
./start.sh up        # 啟動所有服務（含建構映像）
./start.sh down      # 停止所有服務
./start.sh restart   # 重啟所有服務
./start.sh logs      # 查看所有日誌（Ctrl+C 退出）
./start.sh logs csp  # 只看 CSP 後端日誌
./start.sh status    # 查看容器狀態
./start.sh build     # 重新建構映像（不啟動）
./start.sh shell     # 進入後端容器 shell
```

---

## 專案結構

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── api/              # API 路由
│   │   │   ├── auth.py       # 登入、註冊、OIDC Callback、密碼
│   │   │   ├── users.py      # 使用者 CRUD + allowlist + 核准
│   │   │   ├── api_keys.py   # API Key CRUD + regenerate
│   │   │   ├── models.py     # 模型管理 + 健康檢查
│   │   │   ├── departments.py
│   │   │   ├── auth_providers.py  # LDAP / OIDC Provider 設定
│   │   │   ├── alerts.py
│   │   │   ├── audit_logs.py
│   │   │   ├── usage.py
│   │   │   ├── platform_links.py
│   │   │   └── proxy.py      # OpenAI 相容代理
│   │   ├── models/           # SQLAlchemy ORM 模型
│   │   │   ├── user.py       # User + UserModelPermission
│   │   │   ├── api_key.py    # ApiKey + ApiKeyModelPermission
│   │   │   ├── model_registry.py
│   │   │   ├── department.py
│   │   │   ├── auth_provider.py
│   │   │   ├── external_identity.py
│   │   │   ├── audit_log.py
│   │   │   ├── alert.py
│   │   │   ├── token_usage.py
│   │   │   └── platform_link.py
│   │   ├── schemas/          # Pydantic 請求/回應 schema
│   │   ├── services/         # 業務邏輯
│   │   │   ├── auth_service.py      # JWT 驗證、Token 版本控制
│   │   │   ├── external_auth_service.py  # LDAP / OIDC 認證
│   │   │   ├── audit_service.py
│   │   │   ├── alert_service.py
│   │   │   ├── api_key_service.py
│   │   │   ├── health_checker.py    # 背景健康檢查
│   │   │   ├── usage_writer.py      # 批次寫入用量
│   │   │   ├── auto_seed.py         # 啟動時自動建立 admin / 模型 / 連結
│   │   │   └── startup_migrations.py  # SQLite→PG 升級 + 欄位 backfill
│   │   ├── middleware/
│   │   │   └── api_key_auth.py
│   │   ├── utils/
│   │   ├── config.py
│   │   ├── database.py
│   │   └── main.py
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── views/            # 頁面元件
│   │   │   ├── DashboardView.vue
│   │   │   ├── ApiKeysView.vue
│   │   │   ├── ModelsView.vue
│   │   │   ├── UsageView.vue
│   │   │   ├── UsersView.vue
│   │   │   ├── DepartmentsView.vue
│   │   │   ├── AlertsView.vue
│   │   │   ├── AuditLogsView.vue
│   │   │   ├── AuthProvidersView.vue
│   │   │   ├── PlatformLinksView.vue
│   │   │   └── LoginView.vue
│   │   ├── components/
│   │   │   ├── layout/       # AppHeader、AppSidebar、AppLayout
│   │   │   ├── charts/       # ECharts 圖表元件
│   │   │   ├── common/       # ConfirmDialog
│   │   │   └── dashboard/    # PlatformCard、UsageSummaryCard
│   │   ├── api/              # Axios API client
│   │   ├── stores/           # Pinia stores
│   │   └── router/
│   └── package.json
├── docker/
│   ├── Dockerfile            # 多階段建構（Node + Python）
│   ├── docker-compose.yml
│   └── nginx.conf            # Nginx 反向代理配置
├── start.sh
├── .env.example
└── README.md
```

---

## HTTPS 啟用

1. 將憑證放入 `docker/certs/` 目錄（`server.crt` + `server.key`）
2. 編輯 `docker/docker-compose.yml`，取消 SSL port 和 certs volume 的註解
3. 編輯 `docker/nginx.conf`，取消 HTTPS server block 的註解
4. 重啟服務：`./start.sh restart`

---

## 授權

內部使用專案。
