# myCSPPlatform（CSP — Control & Data Plane）

> ANILA 平台的權威核心服務：掌管使用者、API Key、模型 / Agent 註冊、對話、知識庫與審計，並對外提供 OpenAI 相容代理。

> English version：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本服務存在於所有 ANILA 部署分支。各分支的部署對象 / 認證 / 差異見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。**認證模式依分支而定**：`main` 與多數分支為**純帳密**（RS256 JWT + cookie，`/api/auth/*` 只有 register/login/refresh/logout/me/password/revoke/revocations）；只有 **`prod-intranet-card`** 額外含 SSO(OIDC) + 中科院 PKI 自然人憑證卡（`/api/auth/card/*`）等 fork（main 上的 SSO / `local_password_disabled` 欄位已於 migration `0035` 移除）。

---

## 簡介

`myCSPPlatform`（程式內代號 **CSP**）是 ANILA 的「真相來源」（authoritative store）：Router、ingestion-worker、anila-studio、各前端都向它要使用者身分、API Key、模型 / Agent manifest 與用量資料。它同時跑兩個平面：

- **Control Plane** — `/api/*`（RS256 JWT / cookie 認證）：管理介面與平台內部溝通。掌管使用者、API Key、模型註冊、Agent 註冊與核准、對話 / 附件 / 分享 / 交接、審計、告警、banners、部門、平台連結、trusted-hosts、使用者記憶、service token / service clients 等。
- **Data Plane** — `/v1/*` 與 `/v2/*`（`sk-` API Key 或 cookie）：OpenAI 相容代理，依 `model_type` 路由到後端 LLM / Embedding / VLM / Agent，並統一寫 `token_usage` 計費。

CSP 另承載一條應用管線並對接一個已抽離的服務：

- **Ingestion 知識庫** — 文件上傳 → 切塊 → embedding → pgvector 檢索（RAG）+ 跨文件關係。CSP 透過 `arq` 把工作推進 Redis 佇列，由獨立的 [`ingestion-worker`](../ingestion-worker/) container 消化。
- **Studio 簡報 / 報告生成** — **已抽出至獨立服務 [`anila-studio`](../anila-studio/)**（含 FLUX 生圖、Graphviz 圖表渲染、PPTX/報告管線）。CSP 端**只保留 contract endpoint**：ingestion `/search`、`/images/search`、`/images/{id}/blob`、`/api/proxy` LLM、`/.well-known/jwks.json`、`/api/auth/revocations`，以及 Redis token-revoke publisher。決策見 [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md)。

> 平台整體定位見根目錄 [`../README.md`](../README.md) 與唯一規劃文件 [`../anila_plan.md`](../anila_plan.md)。

---

## 架構與技術棧

```
                       ┌──────────────┐
        使用者 / SDK ─▶│    Nginx     │ 對外入口（反向代理 + 靜態 SPA + 安全 header）
        / Router       └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │   FastAPI    │ csp :8000  (app.main:app)
                       │  /api/*  ──── Control Plane (RS256 JWT / cookie)
                       │  /v1,/v2 ──── Data Plane    (sk- API Key)
                       └──┬────┬───┬──┘
              ┌───────────┘    │   └────────────┐
        ┌─────▼──────┐  ┌──────▼──────┐  ┌──────▼────────┐
        │  postgres   │  │  Redis      │  │ 模型 / Agent   │
        │ (pgvector)  │  │ (arq+pub/sub)│ │  endpoints     │
        └─────────────┘  └──────┬──────┘  └────────────────┘
                                │ enqueue
                         ┌──────▼──────────┐
                         │ ingestion-worker │ (獨立 container)
                         └──────────────────┘
```

### 後端 `backend/`（FastAPI / Python）

| 項目 | 內容（取自 `requirements.txt` / `docker/Dockerfile`） |
|------|------|
| 語言 / 框架 | Python 3.11 · FastAPI 0.115.6 · uvicorn[standard] 0.34.0 |
| ORM / migration | SQLAlchemy 2.0.36 · Alembic 1.14.1（migrations `0001`–`0045`） |
| 設定 | pydantic-settings 2.7.1 |
| 認證 | **JWT 為 RS256**（非對稱，`app/utils/security.py` + JWKS；`python-jose[cryptography] 3.3.0`）· passlib[bcrypt] 1.7.4 + bcrypt 4.0.1（pin）。**LDAP 已移除** |
| 資料庫驅動 | psycopg2-binary 2.9.10（PostgreSQL 16 + pgvector）+ asyncpg（`csp_app` RLS pool，ingestion 用） |
| HTTP client | httpx 0.28.1（代理下游模型 / agent） |
| 佇列 | arq 0.26.1（ingestion / eval / relation-reresolve 推進 Redis）+ Redis pub/sub（token revoke） |
| 文字後處理 | opencc-python-reimplemented 0.1.7 |
| 測試 | pytest · pytest-asyncio 0.24.0 · respx 0.22.0（約 40 個測試檔） |

> 容器走 `docker/Dockerfile`（multi-stage，含 `anila-core[rag]`），system 套件 `gcc` / `libpq-dev` / `curl` / `graphviz` / `fonts-noto-cjk`。`backend/Dockerfile` 已 dead（compose 用 `docker/Dockerfile`）。**JWT 簽署為 RS256**：`ALGORITHM=HS256` 設定為 legacy、不再用於 access/refresh。

### 前端 `frontend/`（Vue 3 / Vite，package `csp-platform`）

| 項目 | 內容 |
|------|------|
| 框架 | Vue 3.5.13 + Vite 6.0.5 |
| 狀態 / 路由 | Pinia 2.3.0 · vue-router 4.5.0 |
| HTTP / 圖表 | axios 1.7.9 · ECharts 5.5.1 + vue-echarts 7.0.3 · **cytoscape 3.34.0**（`RelationGraph.vue` 關係圖） |
| 樣式 | Tailwind 3.4.17 + PostCSS 8.4.49 |

純 SPA 管理介面（29 個 view：dashboard / API Key / 模型 / 使用者 / 用量 / Developer agents / trusted-hosts / 關係圖等），由 Nginx 提供靜態檔。

---

## 目錄結構

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── main.py            # lifespan：startup_security → alembic upgrade → startup_migrations
│   │   │                      #   → auto_seed → trusted_host backfill → health_checker / usage_writer
│   │   │                      #   / ingestion_pool；CORS / TrustedHost / CSRF / SPA fallback
│   │   ├── config.py          # pydantic-settings Settings
│   │   ├── database.py
│   │   ├── api/               # router.py 匯總；各資源 router（見「API 介面」）
│   │   │   └── ingestion/     # collections / credentials / documents / eval_runs /
│   │   │                      #   image_blob / jobs / preview / relations / search
│   │   ├── models/            # 23 個 ORM 檔（user / agent / model_registry / ingestion /
│   │   │                      #   token_usage / audit_log / banner / department / ...）
│   │   ├── schemas/
│   │   ├── services/          # 25 個 service（auth / proxy / health_checker / usage_writer /
│   │   │                      #   auto_seed / startup_security / ingestion_queue /
│   │   │                      #   trusted_host / token_revocation_publisher / agent_credential ...）
│   │   ├── middleware/        # api_key_auth · caller · cookies · csrf
│   │   └── utils/             # security.py（RS256 JWT + JWKS keys）· time_helpers.py
│   ├── migrations/versions/   # Alembic 0001..0045
│   ├── tests/                 # ~40 pytest 檔
│   ├── scripts/generate-jwt-keypair.py
│   ├── requirements.txt
│   └── Dockerfile             # dead（compose 用 docker/Dockerfile）
├── frontend/                  # Vue 3 SPA（src/views 29 個 · components/cli 設計系統）
├── docker/                    # Dockerfile（真正用的）· docker-compose.yml（單獨 csp+nginx+postgres）· nginx.conf
├── scripts/init_db.py · start.sh · .env.example
└── README.md / README.en.md
```

---

## 啟動與部署

### 整合跑法（建議）— 作為 ANILA stack 的 `csp` 服務

完整 stack（含 redis / ingestion-worker / router / anila-studio / 前端 / nginx）定義在 **repo 根目錄** 的 compose：

```bash
# 從 repo 根目錄
docker compose -f docker-compose-dev.yml up -d --build csp   # dev
# 或 prod：docker compose up -d csp（prod 分支可用 scripts/deploy-prod.sh）
```

CSP 連兩個 network：`default`（stack 內部）與 `anila-models-net`（external，打 `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`）。第一次啟動若 `anila-models-net` 不存在：`docker network create anila-models-net`。

> `myCSPPlatform/docker/docker-compose.yml` 是**單獨開發**用的精簡 compose（只有 `nginx` + `postgres` + `csp`，無 redis / router）；要完整功能請用 repo 根目錄 compose。

### CSP 單獨開發

```bash
cd myCSPPlatform
cp .env.example .env       # 至少改 SECRET_KEY 與 ADMIN_PASSWORD
./start.sh up              # up / down / restart / logs [csp] / status / build / shell
```

後端本地（不經容器，需自備 PostgreSQL）：`cd backend && uvicorn app.main:app --port 8000`。

### 關鍵環境變數（取自 `config.py`，預設值如實）

| 變數 | 預設 | 說明 |
|------|----------|------|
| `APP_NAME` / `APP_VERSION` | `CSP Platform` / `1.0.0` | 服務識別；`/health` 回報版本 |
| `DEBUG` | `False` | debug 旗標 |
| `ENABLE_API_DOCS` | `False` | 為 true 才掛 `/docs` + `/openapi.json` |
| `ENABLE_PUBLIC_SHARE` | `True` | 開放未認證的 `/api/public/share/{token}` |
| `DATABASE_URL` | `postgresql://csp:csp_password@localhost:5432/csp`（compose 設 `@postgres:5432`） | DB 連線 |
| `SECRET_KEY` | `your-secret-key-change-this-in-production` | **不再簽 access/refresh JWT**（已 RS256）；供 startup_security 與 credential_crypto |
| `ALGORITHM` | `HS256` | **legacy / 未使用**（access/refresh 走 RS256） |
| `JWT_PRIVATE_KEY_PATH` / `JWT_PUBLIC_KEY_PATH` / `JWT_KID` | `secrets/jwt-private.pem` / `secrets/jwt-public.pem` / `anila-v1` | RS256 金鑰 + JWKS kid |
| `ALLOW_AUTO_KEYGEN` | `False` | 缺金鑰時自動產生（**僅 dev/test**） |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | `60` / `30` | JWT 效期 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `changeme` | seed admin；prod 必覆寫 |
| `CSP_SERVICE_TOKEN` | `""` | legacy fleet-shared s2s token（fallback） |
| `MODEL_GATEWAY_API_KEY` | `""` | 外呼 model gateway 注入的 Bearer（僅模型呼叫，不含 agent dispatch） |
| `EMBEDDING_TIMEOUT` / `LLM_TIMEOUT` | `30` / `120` | proxy 逾時（秒） |
| `PROXY_MAX_RETRIES` / `PROXY_RETRY_BASE_DELAY` | `3` / `0.5` | proxy 重試 |
| `ALLOWED_ORIGINS` / `ALLOWED_HOSTS` / `COOKIE_SECURE` | 見 config | CORS allowlist / Host allowlist（`*`=不啟用）/ cookie secure |
| `AUTO_REGISTER_MODELS` / `AUTO_REGISTER_AGENTS` / `AUTO_REGISTER_LINKS` / `AUTO_SEED_API_KEYS` | `""` | 啟動時宣告式 seed |
| `ATTACHMENT_STORAGE_PATH` | `data/attachments` | 附件儲存 |

直接由 `os.environ` 讀（不在 config.py）：`ANILA_ALLOW_DEV_SECRET`、`INTERNAL_PLATFORM_API_KEY`、`ANILA_TRUSTED_HOSTS`、`REDIS_URL`（`ingestion_queue` 預設 `redis://redis:6379`、`token_revocation_publisher` 預設 `redis://redis:6379/0`）、`INGESTION_UPLOAD_DIR`（`/var/anila/ingestion-uploads`）。

> **`prod-intranet-card` 另有** `ENABLE_CARD_LOGIN` / `REQUIRE_CARD_LOGIN_ONLY` / `CARD_INITIAL_OWNERS` 等卡登入變數（見該分支根 README）；這些在 `main` 不存在。

---

## API 介面

**Control Plane（`/api/*`）**：`/api/auth`（register / login / refresh / logout / me / password / revoke / **revocations** — main 上**無 card/SSO/OIDC**）、`/api/keys`、`/api/models`（含 `set-router-primary` / `activate` / `purge`）、`/api/agents`（register / approve / reject / encryption / runtime-config / health-check / credentials / template/download）、`/api/users`、`/api/departments`、`/api/usage`、`/api/alerts`、`/api/audit-logs`、`/api/banners`、`/api/memory`、`/api/platform-links`、`/api/service-clients`、`/api/service-access-grants`、`/api/trusted-hosts`、`/api/conversations`（含 `/search`、shares、ratings）、`/api/attachments`、`/api/handoffs` + `/api/notifications`、`/api/public/share/{token}`（未認證，受 `ENABLE_PUBLIC_SHARE` 控）、`/api/ingestion/*`。

**Data Plane（`/v1/*`、`/v2/*`，`app/api/proxy.py`）**：`GET /v1/agents`（Router 取 agent manifest）、`GET /v1/models`（權限過濾的模型清單）、`POST /v1/chat/completions`（agent-first 後 model，串流 + 非串流，記憶注入，classified latch）、`POST /v1/agents/{name}/sessions/{sid}/answer`（Router resume passthrough）、`POST /v1/embeddings`、`POST /v2/embeddings`。

**其他**：`GET /.well-known/jwks.json`（RFC 7517，未認證，`max-age=3600`）、`GET /health`、`GET /docs`+`/openapi.json`（僅 `ENABLE_API_DOCS=true`）、SPA catch-all（含路徑遍歷防護）。

代理使用範例：

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemma4","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

---

## 與其他服務的關係 / 認證

- **被呼叫**：Router 拉 `/v1/agents` 並以 service token 分派；anila-studio 走 contract endpoint（search / image-blob / JWKS / `/api/auth/revocations`，並用 JWKS 驗 CSP JWT）；ingestion-worker 共用 DB / 佇列；前端走 `/api/*` + `/v1/*`。
- **外呼**：已註冊模型 / 已核准 agent endpoint（httpx + 呼叫時 SSRF guard + per-agent service token）；model gateway（Bearer `MODEL_GATEWAY_API_KEY`）；Redis（arq + pub/sub）；Postgres + pgvector。引入 `anila_core` 做 SSRF guard / credential crypto / memory adapter / relation resolution / parser / pg pool。
- **認證機制**：使用者 RS256 JWT（access + refresh，`tv` token-version 撤銷 claim）走 Bearer 或 `anila_access_token` cookie；使用者 API Key `sk-`；cookie session（`anila_access_token` / `anila_refresh_token` / `anila_csrf`）+ double-submit CSRF（`X-CSRF-Token`，constant-time）；s2s token `bsk-`（單次 bootstrap）/ `csk-`（輪替 agent）/ service_clients（AES-256-GCM envelope + sha256 lookup hash + `hmac.compare_digest`）+ legacy `CSP_SERVICE_TOKEN` fallback。

---

## 安全設計要點

- **Classified 單向閂鎖**：agent `requires_encryption` → 對話 `classified=TRUE`（`proxy.py`）；記憶引用加密來源時依 Bell-LaPadula「no write down」latch（`ConversationMemoryChunk.is_encrypted`）；只升不降，declassify route 已移除（Phase K），classified 對話不可分享。
- **SSRF guard**：`anila_core.security.validate_outbound_url` 在呼叫時把關（proxy 502 / health_checker offline / memory 注入 gateway key 前）；allow-list 由 `trusted_hosts` 表 + `ANILA_TRUSTED_HOSTS` env（30s TTL cache）。
- **startup_security**：`assert_no_dev_defaults()` 在 prod 對 `SECRET_KEY` / `ADMIN_PASSWORD` / `CSP_SERVICE_TOKEN` / DB password / `INTERNAL_PLATFORM_API_KEY` / `CODESERVER_PASSWORD` 的 dev 預設值拒絕啟動（空 `SECRET_KEY` 永遠 fatal）；`ANILA_ALLOW_DEV_SECRET=1` 降為 warn。
- **Credential 加密**：AES-256-GCM（anila-core `credential_crypto` / `service_token_envelope`）。
- **Token 撤銷**：持久 `token_revocations` 表 + JWT `tv` 強制 + Redis fan-out；`/api/auth/revocations` cold-start sync。
- **入站 hardening**：CORS allowlist（無 `*` fallback）、選用 TrustedHostMiddleware、double-submit CSRF、SPA 路徑遍歷防護、nginx 安全 header + rate-limit。

---

## 相關文件

- Ingestion 平台設計：[`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md) · Parent-child RAG：[`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- 多服務整合：[`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md) · Service-token cutover：[`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- anila-studio 抽出：[`../docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md)
- 平台整體：[`../README.md`](../README.md) · 路線圖：[`../anila_plan.md`](../anila_plan.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

---

**Role**：Control + Data Plane · **Authoritative for**：users · api_keys · models · agents · service_clients · token_usage · audit_logs · ingestion 知識庫（Studio + FLUX + 圖表渲染已抽到 anila-studio，CSP 僅保留 contract endpoint）
