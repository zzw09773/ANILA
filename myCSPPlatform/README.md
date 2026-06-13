# myCSPPlatform（CSP — Control & Data Plane）

> ANILA 平台的權威核心服務：掌管使用者、API Key、模型 / Agent 註冊、對話與審計，並對外提供 OpenAI 相容代理。

> English version：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本服務存在於所有 ANILA 部署分支。各分支的部署對象 / 認證 / 差異見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。**認證模式依分支而定**：`main` / `prod-public-passwd` / `prod-military-passwd` / `dev-*` / `trial-military` 為**純帳密**；`prod-intranet-card` 額外含 **SSO(OIDC) + 中科院 PKI 自然人憑證卡**（`/api/auth/card/*`）、`users.local_password_disabled` SSO-only、`GET /api/auth/revocations`（anila-studio cold-start sync 用）——這些是該分支的永久 fork 區，不回推 main。

---

## 簡介

`myCSPPlatform`（程式內代號 **CSP**）是 ANILA 的「真相來源」（authoritative store）：Router、ingestion-worker、anila-studio、各前端都向它要使用者身分、API Key、模型 / Agent manifest 與用量資料。它同時扮演兩個平面：

- **Control Plane** — `/api/*`（JWT / cookie 認證）：管理介面與平台內部溝通，掌管使用者、API Key、模型註冊、Agent 註冊與核准、對話 / 附件 / 分享 / 交接、審計、告警、trusted-hosts、使用者記憶、service token 等。
- **Data Plane** — `/v1/*`（`sk-` API Key 或 cookie）：OpenAI 相容代理（proxy），任何 OpenAI SDK / curl 可直接把 CSP 當 endpoint，依 `model_type` 路由到後端 LLM / Embedding / VLM / Agent，並統一寫 `token_usage` 計費。

CSP 另外承載一條應用管線、並對接一個已抽離的服務：

- **Ingestion 知識庫** — 文件上傳 → 切塊 → embedding → pgvector 檢索（RAG）。CSP 透過 `arq` 把工作推進 Redis 佇列，由獨立的 [`ingestion-worker`](../ingestion-worker/) container 消化。
- **Studio 簡報生成** — **已於 2026-05-23（PR #12）抽出至獨立服務 [`anila-studio`](../anila-studio/)**。CSP 端只保留 contract endpoint：`/api/ingestion/.../search`、`/images/search`、`/images/{id}/blob`、`/.well-known/jwks.json`、`/api/auth/revocations`，以及 Redis token-revoke publisher。決策見 [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md)。

> 平台整體定位見根目錄 [`../README.md`](../README.md) 與唯一規劃文件 [`../anila_plan.md`](../anila_plan.md)。

---

## 架構與技術棧

```
                       ┌──────────────┐
        使用者 / SDK ─▶│    Nginx     │ 對外入口（反向代理 + 靜態 SPA + 6 安全 header）
        / Router       └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │   FastAPI    │ csp :8000  (app.main:app)
                       │  /api/*  ──── Control Plane (JWT / cookie)
                       │  /v1/*   ──── Data Plane    (sk- API Key)
                       └──┬────┬───┬──┘
              ┌───────────┘    │   └────────────┐
        ┌─────▼──────┐  ┌──────▼──────┐  ┌──────▼────────┐
        │  csp-db     │  │  Redis      │  │ 模型 / Agent   │
        │ (pgvector)  │  │ (arq queue) │  │  endpoints     │
        └─────────────┘  └──────┬──────┘  └────────────────┘
                                │ enqueue
                         ┌──────▼──────────┐
                         │ ingestion-worker │ (獨立 container)
                         └──────────────────┘
```

### 後端 `backend/`（FastAPI / Python）

| 項目 | 內容 |
|------|------|
| 語言 / 框架 | Python 3.11 · FastAPI · Uvicorn[standard] |
| ORM / migration | SQLAlchemy 2.0 · Alembic |
| 資料庫驅動 | psycopg2-binary（PostgreSQL 16 + pgvector） |
| 認證 | python-jose（JWT, HS256）· passlib[bcrypt]；`prod-intranet-card` 另含 OIDC + PKCS#7/CMS 卡驗章 |
| HTTP client | httpx（代理下游模型 / agent） |
| 佇列 | arq（把 ingestion 工作推進 Redis） |
| 文字後處理 | opencc-python-reimplemented（簡→繁 + 台灣用語） |
| 圖像 / 圖表 | FLUX backend（HTTP）· Graphviz `dot`（渲染 `Slide.diagram_dot`） |
| 測試 | pytest · pytest-asyncio · respx（mock FLUX backend） |

完整套件見 [`backend/requirements.txt`](./backend/requirements.txt)。後端容器額外裝 `graphviz` 與 `fonts-noto-cjk`，讓含繁中標籤的 DOT 圖正常渲染（見 [`backend/Dockerfile`](./backend/Dockerfile)）。

### 前端 `frontend/`（Vue 3 / Vite）

| 項目 | 內容 |
|------|------|
| 框架 | Vue 3（`^3.5`）+ Vite 6 |
| 狀態 / 路由 | Pinia · vue-router |
| HTTP / 圖表 | axios · ECharts + vue-echarts（用量時序） |
| 樣式 | Tailwind CSS + PostCSS |

前端是純 SPA 管理介面（dashboard / API Key / 模型 / 使用者 / 用量 / Developer agents / trusted-hosts 等），由 Nginx 提供靜態檔。依套件見 [`frontend/package.json`](./frontend/package.json)。

---

## 目錄結構

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app 入口（app.main:app）
│   │   ├── config.py              # pydantic-settings 設定
│   │   ├── database.py            # SQLAlchemy engine / session
│   │   ├── api/
│   │   │   ├── router.py          # 匯總所有 /api/* 子路由
│   │   │   ├── auth.py users.py api_keys.py models.py agents.py
│   │   │   ├── proxy.py           # /v1/* OpenAI 相容代理（Data Plane）
│   │   │   ├── conversations.py attachments.py public_share.py handoffs.py
│   │   │   ├── memory.py          # /api/memory 使用者記憶
│   │   │   ├── service_clients.py service_access_grants.py trusted_hosts.py
│   │   │   └── ingestion/         # 知識庫 RAG：documents / jobs / search / ...
│   │   ├── models/                # SQLAlchemy ORM（users / api_key / model_registry /
│   │   │                          #   agent / ingestion / token_usage / audit_log ...）
│   │   ├── schemas/               # Pydantic schema
│   │   ├── services/              # auth / proxy / health_checker / usage_writer /
│   │   │                          #   auto_seed / ingestion_queue / startup_security ...
│   │   ├── middleware/api_key_auth.py
│   │   └── utils/
│   ├── migrations/                # Alembic 版本
│   ├── tests/                     # pytest（FLUX / agent credential / cookie auth ...）
│   ├── requirements.txt
│   └── Dockerfile                 # python:3.11-slim + graphviz + noto-cjk
├── frontend/                      # Vue 3 SPA（src / package.json / vite.config.js）
├── docker/                        # 單獨開發用 Dockerfile / docker-compose.yml / nginx.conf
├── scripts/
├── start.sh                       # 單獨開發用啟動腳本
├── .env.example
└── README.md / README.en.md
```

---

## 啟動與部署

### 整合跑法（建議）— 作為 ANILA stack 的 `csp` 服務

從 **repo 根目錄**用 compose 啟動；CSP 在 stack 裡是 `csp` 服務：

```bash
# 從 repo 根目錄
docker compose -f docker-compose-dev.yml up -d --build csp   # dev
# 或 prod：docker compose up -d csp（prod 分支可用 scripts/deploy-prod.sh）
```

stack 同時含 `csp-db`（pgvector/pg16）、`redis`、`ingestion-worker`、`router`、`pptx-renderer`、`anilalm`、`anila-ui`、`nginx`。CSP 連兩個 network：`default`（stack 內部）與 `anila-models-net`（external，打 `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`）。

> 第一次啟動若 `anila-models-net` 不存在：`docker network create anila-models-net`。

### CSP 單獨開發

```bash
cd myCSPPlatform
cp .env.example .env       # 至少改 SECRET_KEY 與 ADMIN_PASSWORD
./start.sh up              # up / down / restart / logs [csp] / status / build / shell
```

後端本地（不經容器，需自備 PostgreSQL）：

```bash
cd myCSPPlatform/backend && source .venv/bin/activate
pytest
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 關鍵環境變數

| 變數 | dev 預設 | 說明 |
|------|----------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | 應用 DB 連線（`csp_app` 受 RLS 約束） |
| `MIGRATION_DATABASE_URL` | `postgresql://csp:csp@csp-db:5432/csp` | migration 用較高權限帳號 |
| `SECRET_KEY` | `dev-secret-key-change-in-prod` | JWT 簽署密鑰，**正式環境務必修改**（否則 `startup_security` 拒絕啟動） |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | JWT 設定 |
| `CSP_SERVICE_TOKEN` | `dev-service-token` | legacy fleet-shared s2s token（fallback）；每支 agent 現走自己的 `csk-` |
| `REDIS_URL` | `redis://redis:6379` | arq ingestion 佇列 |
| `INGESTION_UPLOAD_DIR` | `/var/anila/ingestion-uploads` | 上傳檔暫存目錄 |
| `AUTO_REGISTER_MODELS` / `AUTO_REGISTER_AGENTS` | 見 compose | 啟動時宣告式註冊模型 / agent |
| `AUTO_SEED_API_KEYS` | 見 compose | 啟動時建立種子使用者 + `sk-` key |
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | 啟用 FLUX 圖像；空字串 = 關閉 |
| `FLUX_MAX_CONCURRENT` / `FLUX_TIMEOUT_SECONDS` | `4` / `180` | FLUX 並發 / 逾時 |
| `ANILA_TRUSTED_HOSTS` | `gemma4,gpt-oss-20b,nv-embed-proxy,host.docker.internal` | SSRF guard allow-list bootstrap（之後由 `/trusted-hosts` UI 管） |

> `FLUX_CACHE_DIR` 未設時預設 `$INGESTION_UPLOAD_DIR/flux-cache`；FLUX 圖以 `SHA256(prompt + aspect_ratio)` 內容定址快取。**`prod-intranet-card` 另有 `ENABLE_CARD_LOGIN` / `REQUIRE_CARD_LOGIN_ONLY` / `CARD_INITIAL_OWNERS` 等卡登入變數**（見該分支根 README）。

---

## 與其他服務的關係

| 服務 | 關係 |
|------|------|
| **csp-db**（pgvector/pg16） | 主資料庫，同時供 Ingestion 向量檢索 |
| **redis** | arq 佇列：CSP `enqueue_ingest_document()` 入列，`ingestion-worker` 消費；另作 token-revoke pub/sub |
| **ingestion-worker** | 獨立 container，消化 ingest 工作（切塊 / embedding / 寫 pgvector） |
| **gemma4 / gpt-oss-20b**（LLM） | 經 `anila-models-net` 由 `/v1/chat/completions` 代理；gemma4 為 Router primary |
| **nv-embed-proxy**（Embedding） | `/v1/embeddings` 代理目標；Ingestion 也用它做 embedding |
| **flux2-dev / flux2-dev-agent**（FLUX） | `image-generator` agent；Studio / 聊天繪圖經此產圖 |
| **router**（anila-core-router） | 向 CSP 拉 `/v1/agents` manifest 分派；以 service token 認證 |
| **anila-studio** | HTTP-only 呼叫 CSP 的 search / proxy / JWKS / revocations contract endpoint |
| **anilalm / anila-ui** | 前端，透過 `/api/*`（管理 / 對話）與 `/v1/*`（推論）溝通 |

代理使用範例（OpenAI 相容）：

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemma4","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

> **加密單向閂鎖**：若請求路由到標記 `requires_encryption` 的 agent，整段對話會被 latch 成 `classified=true`——這是 ANILA「主 LLM 未加密 → 遇加密 agent 整段升級」的核心安全機制，UI 側無降級路徑。

---

## 相關文件

- Ingestion 平台設計：[`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)
- Parent-child RAG 設計：[`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- 多服務整合計畫：[`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md)
- SSO 遷移：[`../docs/platform/sso-migration.md`](../docs/platform/sso-migration.md)
- Service-token cutover：[`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- Studio / FLUX 規格：[`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- 平台整體：[`../README.md`](../README.md) · 路線圖：[`../anila_plan.md`](../anila_plan.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

---

**Role**：Control + Data Plane · **Authoritative for**：users · api_keys · models · agents · service_clients · token_usage · audit_logs · ingestion 知識庫（Studio 已抽到 anila-studio，CSP 僅保留 contract endpoint）
