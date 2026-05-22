# myCSPPlatform（CSP — Cloud Service Platform）

> ANILA 平台的 Control Plane 與 Data Plane：權威掌管使用者、API Key、模型 / Agent 註冊，並對外提供 OpenAI 相容代理。

> English version: [`README.en.md`](./README.en.md)

---

## 簡介 / Overview

`myCSPPlatform`（程式內代號 **CSP**）是 ANILA 平台的核心服務。它同時扮演兩個角色：

- **Control Plane** — `/api/*`（JWT / cookie auth）：管理介面與平台內部溝通。掌管使用者、API Key、模型註冊、Agent 註冊與核准、對話 / 附件 / 分享 / 交接、審計、告警等。
- **Data Plane** — `/v1/*`（`sk-` API Key）：OpenAI 相容代理（proxy），讓任何 OpenAI SDK / curl 可以直接把 CSP 當 endpoint 使用，並依 `model_type` 路由到後端 LLM / Embedding / VLM / Agent。

除了上述「平台治理」職能，CSP 也承載兩條應用管線：

- **Ingestion 知識庫** — 文件上傳 → 切塊 → embedding → pgvector 檢索（RAG）。CSP 透過 `arq` 把 ingest 工作丟進 Redis 佇列，由獨立的 `ingestion-worker` container 消化。
- **Studio / ANILALM 簡報生成** — 由 LLM 產生投影片大綱，CSP 端組裝 `.pptx`，並可內嵌 FLUX 生成圖片（`image-generator` agent）與 Graphviz 流程圖。

> 在整個 ANILA 系統中，CSP 是「真相來源」（authoritative store）：Router、Worker、UI 都向它要使用者身分、API Key、模型 / Agent manifest 與用量資料。平台整體定位見 repo 根目錄 [`../README.md`](../README.md) 與 [`../anila_plan.md`](../anila_plan.md)。

---

## 架構與技術棧 / Architecture & Stack

```
                       ┌──────────────┐
        使用者 / SDK ─▶│    Nginx     │ dev 入口（反向代理 + 靜態 SPA）
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
| 認證 | python-jose（JWT, HS256）· passlib[bcrypt] |
| HTTP client | httpx（代理下游模型 / agent） |
| 佇列 | arq（把 ingestion 工作丟進 Redis） |
| 文字後處理 | opencc-python-reimplemented（簡→繁 + 台灣用語，Studio 用） |
| 圖像 / 圖表 | FLUX backend（HTTP）· Graphviz `dot`（系統套件，渲染 `Slide.diagram_dot`） |
| 測試 | pytest · pytest-asyncio · respx（mock FLUX backend） |

完整套件見 [`backend/requirements.txt`](./backend/requirements.txt)。後端容器額外安裝 `graphviz` 與 `fonts-noto-cjk`，讓含繁體中文標籤的 DOT 圖能正常渲染（見 [`backend/Dockerfile`](./backend/Dockerfile)）。

### 前端 `frontend/`（Vue 3 / Vite）

| 項目 | 內容 |
|------|------|
| 框架 | Vue 3（`^3.5`）+ Vite 6 |
| 狀態管理 | Pinia |
| 路由 | vue-router |
| HTTP | axios |
| 圖表 | ECharts + vue-echarts（用量時序圖） |
| 樣式 | Tailwind CSS + PostCSS |

依套件見 [`frontend/package.json`](./frontend/package.json)。前端是純 SPA 管理介面（dashboard / API Key / 模型 / 使用者 / 用量 / Developer agents 等），由 Nginx 提供靜態檔。

---

## 目錄結構 / Layout

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
│   │   │   ├── studio.py          # /api/studio 簡報生成
│   │   │   └── ingestion/         # 知識庫 RAG：documents/jobs/search/...
│   │   ├── models/                # SQLAlchemy ORM（users, api_key, model_registry,
│   │   │                          #   agent, ingestion, token_usage, audit_log, ...）
│   │   ├── schemas/               # Pydantic schema
│   │   ├── services/              # auth/proxy/health_checker/usage_writer/auto_seed
│   │   │                          #   ingestion_queue, studio_job_service,
│   │   │                          #   flux_image_provider, diagram_renderer, ...
│   │   ├── middleware/api_key_auth.py
│   │   └── utils/
│   ├── migrations/                # Alembic 版本
│   ├── tests/                     # pytest（含 FLUX / agent credential / cookie auth）
│   ├── requirements.txt
│   └── Dockerfile                 # python:3.11-slim + graphviz + noto-cjk
├── frontend/
│   ├── src/                       # views / components / stores / api / router
│   ├── package.json
│   ├── vite.config.js · tailwind.config.js
│   └── index.html
├── docker/
│   ├── Dockerfile                 # 單獨開發用 multi-stage（Node + Python）
│   ├── docker-compose.yml         # 僅 CSP 單跑（整合請用 repo 根目錄 compose）
│   └── nginx.conf
├── scripts/
├── start.sh                       # 單獨開發用啟動腳本
├── .env.example
└── README.md / README.en.md
```

---

## 啟動與部署 / Setup & Run

### 整合跑法（建議）— 作為 ANILA dev stack 的 `csp` 服務

CSP 在 dev stack 裡是 `csp` 服務，**從 repo 根目錄**用 `docker-compose-dev.yml` 啟動：

```bash
# 從 repo 根目錄（<project_root>）
docker compose -f docker-compose-dev.yml up -d --build csp
```

dev stack 同時包含 `csp-db`（pgvector/pg16）、`redis`、`ingestion-worker`、`router`、`pptx-renderer`、`anilalm`、`anila-ui`、`nginx` 等服務。CSP 連到兩個 network：`default`（stack 內部）與 `anila-models-net`（external，用來打 `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`）。

> 第一次啟動若 `anila-models-net` 不存在：`docker network create anila-models-net`。

### 後端本地開發（不經容器）

後端 venv 位於 [`backend/.venv`](./backend/.venv)：

```bash
cd myCSPPlatform/backend
source .venv/bin/activate
# 跑測試
pytest
# 本地啟動（需自備 PostgreSQL / 設好 DATABASE_URL）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### CSP 單獨開發（不跑整個 stack）

```bash
cd myCSPPlatform
cp .env.example .env   # 至少改 SECRET_KEY 與 ADMIN_PASSWORD
./start.sh up          # up / down / restart / logs [csp] / status / build / shell
```

### 關鍵環境變數（取自 dev compose / Dockerfile）

| 變數 | dev 預設 | 說明 |
|------|----------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | 應用 DB 連線 |
| `MIGRATION_DATABASE_URL` | `postgresql://csp:csp@csp-db:5432/csp` | migration 用較高權限帳號 |
| `SECRET_KEY` | `dev-secret-key-change-in-prod` | JWT 簽署密鑰，**正式環境務必修改** |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | JWT 設定 |
| `CSP_SERVICE_TOKEN` | `dev-service-token` | service-to-service 共用 token（dev） |
| `REDIS_URL` | `redis://redis:6379` | arq ingestion 佇列 |
| `INGESTION_UPLOAD_DIR` | `/var/anila/ingestion-uploads` | 上傳檔暫存目錄 |
| `AUTO_REGISTER_MODELS` / `AUTO_REGISTER_AGENTS` | 見 compose | 啟動時宣告式註冊模型 / agent |
| `AUTO_SEED_API_KEYS` | 見 compose | 啟動時建立種子使用者 + `sk-` key |
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | 啟用 FLUX 圖像；空字串 = 關閉 |
| `FLUX_MAX_CONCURRENT` / `FLUX_TIMEOUT_SECONDS` | `4` / `180` | FLUX 並發 / 逾時 |
| `ANILA_TRUSTED_HOSTS` | `gemma4,gpt-oss-20b,nv-embed-proxy,host.docker.internal` | 允許代理的下游 host |

> `FLUX_CACHE_DIR` 未設時預設為 `$INGESTION_UPLOAD_DIR/flux-cache`；FLUX 圖以 `SHA256(prompt + aspect_ratio)` 內容定址快取。

---

## 與其他服務的關係 / Integration

| 服務 | 關係 |
|------|------|
| **csp-db**（pgvector/pg16） | CSP 的主資料庫，同時供 Ingestion 的向量檢索使用 |
| **redis** | arq 佇列：CSP `enqueue_ingest_document()` 入列，`ingestion-worker` 消費 |
| **ingestion-worker** | 獨立 container，消化 ingest 工作（切塊 / embedding / 寫 pgvector） |
| **gemma4 / gpt-oss-20b**（LLM） | 經 `anila-models-net` 由 `/v1/chat/completions` 代理；gemma4 為 Router primary |
| **nv-embed-proxy**（Embedding） | `/v1/embeddings`、`/v2/embeddings` 代理目標；Ingestion 也用它做 embedding |
| **flux2-dev / flux2-dev-agent**（FLUX 圖像） | `image-generator` agent；Studio 簡報的 `_hydrate_images` 直接呼叫產圖並內嵌 |
| **router**（anila-core-router） | 向 CSP 拉 `/v1/agents` manifest 做分派；以 service token 認證 |
| **pptx-renderer** | Studio 管線把組好的投影片資料交給它渲染最終 `.pptx` |
| **anilalm / anila-ui** | 前端應用，透過 CSP 的 `/api/*`（管理 / 對話）與 `/v1/*`（推論）溝通 |

代理使用範例（OpenAI 相容）：

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma4", "messages": [{"role":"user","content":"Hello!"}], "stream": true}'
```

> 加密單向閂鎖：若請求路由到標記 `requires_encryption` 的 agent，整段對話會被 latch 成 `classified=true`，這是 ANILA「主 LLM 未加密 → 遇加密 agent 整段升級」的核心安全機制。

---

## 相關文件 / Related docs

以下路徑皆已驗證存在（docs 已重整為主題資料夾）：

- Ingestion 平台設計：[`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)
- Parent-child RAG 設計：[`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- 多服務整合計畫：[`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md)
- SSO 遷移：[`../docs/platform/sso-migration.md`](../docs/platform/sso-migration.md)
- Service-token cutover runbook：[`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- Studio / FLUX 規格：[`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- 平台整體：[`../README.md`](../README.md) · 路線圖：[`../anila_plan.md`](../anila_plan.md)

---

**Role**: Control + Data Plane · **Authoritative for**: users · api_keys · models · agents · service_clients · token_usage · audit_logs · ingestion 知識庫 · Studio 簡報生成
