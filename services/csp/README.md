# services/csp（CSP — Control & Data Plane）

> ANILA 平台的權威核心服務（前身 `myCSPPlatform`）：掌管使用者、API Key、模型 / Agent 註冊、任務主脊椎、全鏈追蹤、四級分類治理、對話、知識庫與審計，並對外提供 OpenAI 相容代理。

> English version：[`README.en.md`](./README.en.md)

> 🧭 **本檔對齊 redesign 後現況**（`anila-redesign` 分支）：`services/ apps/ packages/ infra/` 四分頂層結構、根目錄 compose shim（`compose.yaml` → `infra/compose/platform.yml`）、部署腳本落在 `infra/deployment/{scripts,intranet}/`，以及與 CSP 相關的 Slice 2–9 能力（Task 主脊椎、Full Trace、四級分類、Agent Registry、Model Gateway、Service Registry、Artifact 契約）。設計沿革（收斂紀錄）在 [`docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)：憲章 [`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md) 與本服務主文件 [`03-csp-governance-control-plane.md`](../../docs/anila-redesign-docs/03-csp-governance-control-plane.md)。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。

---

## 0. 一句話定位

CSP 是 ANILA 的「真相來源」（authoritative store）與**雙平面閘道**：Router、ingestion-worker、anila-studio、各前端都向它要身分、API Key、模型 / Agent manifest 與用量。它同時服務產品面的**治理中心**（`apps/csp-governance-ui`）、**任務中心**（Task 主脊椎）、**產出中心**（Artifact 契約）與**專案入口**（Service Registry / 啟動閘道）。

- **Control Plane — `/api/*`**（RS256 JWT / cookie 認證）：治理與平台內部溝通。使用者、API Key、模型 / Agent 註冊與核准、任務、政策裁決、四級分類治理、對話 / 附件 / 分享 / 交接、審計、告警、banners、部門、Service Registry、trusted-hosts、使用者記憶、service token / service clients 等。
- **Data Plane — `/v1/*`、`/v2/*`**（`sk-` API Key 或 cookie / service token）：OpenAI 相容代理，依 `model_type` 路由到後端 LLM / Embedding / VLM / Agent，統一寫 `token_usage` 計費；並收攏 Full Trace span（`POST /v1/traces/{trace_id}/spans`）。

CSP 另承載 **Ingestion 知識庫**（文件 → 切塊 → embedding → pgvector RAG + 跨文件關係，經 `arq` 推 Redis 佇列給獨立的 [`ingestion-worker`](../ingestion-worker/)），並對接已抽離的 [`anila-studio`](../anila-studio/)（簡報 / 報告 / 生圖），CSP 端只保留 contract endpoint 與**持久化的 Artifact job store**。

---

## 1. 架構與技術棧

```
                       ┌──────────────┐
        使用者 / SDK ─▶│    Nginx     │ 對外入口（反向代理 + 靜態 SPA + 安全 header）
        / Router       └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │   FastAPI    │ csp :8000  (app.main:app)
                       │  /api/*  ──── Control Plane（RS256 JWT / cookie）
                       │  /v1,/v2 ──── Data Plane（sk- / service token）
                       └──┬────┬───┬──┘
              ┌───────────┘    │   └────────────┐
        ┌─────▼──────┐  ┌──────▼───────┐  ┌──────▼────────┐
        │  postgres   │  │  Redis       │  │ 模型 / Agent   │
        │ (pgvector)  │  │ (arq+pub/sub)│  │  endpoints     │
        └─────────────┘  └──────┬───────┘  └────────────────┘
                                │ enqueue
                         ┌──────▼───────────┐
                         │ ingestion-worker  │（獨立 container）
                         └───────────────────┘
```

| 項目 | 內容（取自 `requirements.txt` / `infra/docker/csp.Dockerfile`） |
|------|------|
| 語言 / 框架 | Python 3.11 · FastAPI 0.136.1 · uvicorn[standard] 0.34.0 |
| ORM / migration | SQLAlchemy 2.0.36 · Alembic 1.14.1（legacy `0001`–`0046`〔無 0025〕接 redesign `r1_0001`–`r1_0008`） |
| 設定 | pydantic-settings 2.7.1（`app/config.py`） |
| 認證 | **JWT 為 RS256**（非對稱，`app/utils/security.py` + JWKS；`python-jose[cryptography] 3.5.0`）· bcrypt 5.0.0（直接使用，cost 12） |
| 資料庫驅動 | psycopg2-binary 2.9.10（PostgreSQL 16 + pgvector）+ asyncpg（`csp_app` RLS pool，ingestion 用） |
| HTTP client | httpx 0.28.1（代理下游模型 / agent） |
| 佇列 | arq 0.26.1（ingestion / eval / relation-reresolve 推進 Redis）+ Redis pub/sub（token revoke） |
| 文字後處理 | opencc-python-reimplemented 0.1.7 |
| 測試 | pytest · pytest-asyncio 0.24.0 · respx 0.22.0 |

> 部署 image 走 [`infra/docker/csp.Dockerfile`](../../infra/docker/csp.Dockerfile)（multi-stage、含 `anila-core[rag]`），而且**只有這一份**——曾經另有一份 compose 從不建的 `services/csp/Dockerfile`，已於 2026-08-06 刪除（[FAKE-CONTROLS](../../docs/FAKE-CONTROLS.md) #50）。前端治理介面已移為頂層 [`apps/csp-governance-ui/`](../../apps/csp-governance-ui/)（Vue 3 / Vite，官方藍視覺改版），由 Nginx 提供靜態檔。
>
> 容器以 **uid 10001（非 root）** 跑。映像裡只有 `/app/logs` 是可寫的；上傳、附件、`share/pki`、`secrets/` 四個都在 bind mount 上，所有權由 host 決定 → 部署前要跑 [`infra/deployment/scripts/fix-runtime-ownership.sh`](../../infra/deployment/scripts/fix-runtime-ownership.sh)（deploy-prod.sh 的 `deploy`/`up`/`rebuild` 與 intranet-deploy.sh `[4c]` 都已接進去）。
>
> ⚠ `secrets/` 的放寬是**白名單**（只有 JWT keypair 與 dev-card-ca bundle）：日後新增「執行期要讀的 secrets 檔」要在那支腳本加一行 `widen_file`，否則讀不到。這樣換到的是不會把未來每一把丟進 `secrets/` 的私鑰都對 gid 10001 開讀。

---

## 2. 模組邊界（`app/modules/`）

redesign 把四個 MVP 核心切成**互不相依**的 module，並以 import-linter 契約（[`.importlinter`](./.importlinter)，`infra/ci/lint-boundaries.sh` 執行）強制：`tasks / policy / launch / artifacts` **彼此不得互相 import**，且 `app.modules.*` **不得反向 import `app.api`**（`api → modules` 單向分層）。

| Module | 檔案 | 職責 |
|--------|------|------|
| `app.modules.tasks` | `router.py` · `service.py` | Task / TaskRun 生命週期（十值狀態機）、SourceSnapshot 三規則、`trace_id` 必產生（doc 01 / doc 03）。 |
| `app.modules.policy` | `router.py` · `service.py` | PolicyDecision **append-only** 裁決紀錄（fail-closed，deny 必附 reason）、ceiling 純函式、四級分類 latch core（`apply_classification` 單向閂鎖；無機密＜營業秘密＜密＜機密）。 |
| `app.modules.launch` | `manifest.py` · `service.py` · `token.py` | Launch Gateway 原語：`service_launches` 落列、啟動 URL、RS256 launch token（doc 07 §6）。**零** policy/task/api 耦合，存取控制由 orchestrator（`app.api.services`）圍事。 |
| `app.modules.artifacts` | `service.py` | Artifact 四表持久化、binding fail-closed、owner-scope 讀面。分類閂鎖與 PolicyDecision 由 orchestrator（`app.api.artifacts`）呼叫 policy 完成。 |

---

## 3. 認證面（`app/api/auth/` 套件）

auth router 已由單檔拆成套件，各認證形態獨立成子模組，全部掛在 `/api/auth` 前綴下（`_common.py`）：

| 子模組 | 路由（`/api/auth` 前綴） | 說明 |
|--------|--------------------------|------|
| `password.py` | `POST /register` · `POST /login` · `POST /refresh` · `POST /logout` · `GET /me` · `PUT /password` | 帳密登入 → RS256 JWT（access + refresh）+ cookie。 |
| `oidc.py` | `GET /providers` · `GET /oidc/{provider_id}/start` · `GET /oidc/{provider_id}/callback` | 企業 SSO / OIDC 授權碼流程（provider 由 `/api/auth-providers` 管）。 |
| `card.py` | `GET /card/challenge` · `POST /card/verify` | 中科院 CSPKI 自然人憑證卡登入：**真** PKCS#7 / CMS 驗簽（SignerInfo 簽章 + 憑證鏈 + nonce 反 replay，`app/services/card_auth.py`）。 |
| `registration_tokens.py` | 一次性註冊 token 面 | 受控自助註冊。 |
| `revocations.py` | `GET /revocations` | service-token 認證的撤銷冷啟同步（anila-studio 消化）。 |

> 三種登入形態（password / oidc / card）在 redesign 樹中**並存於程式碼**，由單一 `ANILA_AUTH_MODE`（`password` / `mixed` / `card-only`）與 SSO provider 是否註冊決定啟用。SSO / OIDC provider 的管理 CRUD 在獨立的 `app/api/auth_providers.py`（前綴 `/api/auth-providers`）。

---

## 4. API 介面：Data Plane vs Control Plane

### Data Plane（`/v1/*`、`/v2/*`）— OpenAI 相容代理 + Trace 收攏

`app/api/proxy.py`（不帶 APIRouter prefix，寫完整路徑，讓 nginx `/v1` 直通吃得到）：

- `GET /v1/agents` — Router 取 agent manifest。
- `GET /v1/models` — 權限過濾的模型清單。
- `POST /v1/chat/completions` — agent-first 後 model；串流 + 非串流；記憶注入；classified 單向閂鎖；**Task 主脊椎**：可帶 `X-ANILA-Task-Id`，命中則對 Task spine 驗證、記 `PolicyDecision(action="task.run")`、以 `TaskRun` 括住代理呼叫；不帶則用量列標記 `legacy_runtime_call=true`（`app/services/proxy/task_link.py`）。
- `POST /v1/agents/{agent_name}/sessions/{session_id}/answer` — Router resume passthrough。
- `POST /v1/embeddings`、`POST /v2/embeddings`。

Full Trace ingest（`app/api/traces.py`，同樣走完整路徑）：

- `POST /v1/traces/{trace_id}/spans` — data-plane span 收攏（`202`，一批 1..256，`(trace_id, span_id)` 冪等 upsert-ignore，fail-safe 不外溢）。認證 = 任一 data-plane 憑證。生產端為 [`anila_trace_sdk`](../../packages/anila-core/src/anila_core/tracing/sdk.py)（`packages/anila-core` 內，fail-open、批次背景 exporter）。
- `GET /api/traces/{trace_id}` — control-plane 讀（admin/owner 或該 trace 所屬任務之申請人）。

### Control Plane（`/api/*`）

- **redesign 新增**：`/api/tasks`（`tasks` module：建立 / 列出 / 取單 / `/{id}/runs`）、`/api/policy-decisions`、`/api/classification/inventory`（機敏盤點）、`/api/classification/declassification-requests`（解密申請 + 主管核准）、`/api/classification-authorities`（機密審批權責）、`/api/services`（Service Registry：CRUD + `/{id}/launch` + `/{id}/audit-callbacks` + `/{id}/manifest` + `/{id}/project-bindings`）、`/api/artifacts`（+ data-plane `POST /v1/artifact-jobs` 等 Studio 回報面）。
- **既有治理面**：`/api/auth`、`/api/auth-providers`、`/api/keys`、`/api/models`（含 `set-router-primary` / `activate` / `purge`）、`/api/agents`（register / approve / reject / health-check / credentials / template）、`/api/users`、`/api/departments`、`/api/usage`、`/api/alerts`、`/api/audit-logs`、`/api/banners`、`/api/memory`、`/api/platform-links`、`/api/service-clients`、`/api/service-access-grants`、`/api/trusted-hosts`、`/api/conversations`（含 `/search`、shares、ratings）、`/api/attachments`、`/api/handoffs` + `/api/notifications`、`/api/public/share/{token}`（未認證，受 `ENABLE_PUBLIC_SHARE` 控）、`/api/ingestion/*`。
- **其他**：`GET /.well-known/jwks.json`（RFC 7517，未認證，`max-age=3600`）、`GET /health`、`GET /docs` + `/openapi.json`（admin tier 才可）、SPA catch-all（含路徑遍歷防護）。

代理使用範例：

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemma4","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

---

## 5. 新資料表 / Migration（`r1_0001`–`r1_0008`，逐檔一行）

redesign 系列接在 legacy 數字鏈之後（`r1_0001` revises `0046`），保持線性；enum 一律開放 `String`（封閉 enum 在 Pydantic 契約層 `app/schemas/contracts/` 把關），JSON 走 `with_variant(JSONB, "postgresql")` 保持可攜。

| Revision | Slice | 內容 |
|----------|-------|------|
| `r1_0001` | 2a | Task / Trace / Policy 六表基礎：`tasks` · `task_runs` · `source_snapshots` · `citations` · `policy_decisions` · `trace_spans`；`classification_level` 預設 `無機密`。 |
| `r1_0002` | 2b-C | `token_usage` ↔ task 連結：`task_id`（FK `ON DELETE SET NULL` + partial index）與 `legacy_runtime_call` 布林旗標（標記無 task 的 `/v1` chat 舊流量）。 |
| `r1_0003` | 3a | 四級分類 schema 升級（無機密／營業秘密／密／機密）+ 治理三表：`classification_events` · `declassification_requests` · `classification_authority_assignments`；於現存資源（conversations / messages / collections / documents …）補四共通分類欄位並 backfill（`classified=true → 機密`；`requires_encryption=true → 密`）。 |
| `r1_0004` | 5a | Agent Registry 升級：`agents.approval_status` 由三值擴為**七值狀態機**（`draft` / `pending_connection_test` / `pending_trace_test` / `pending_security_review` / `approved` / `rejected` / `disabled`），並補 manifest / trace-test / runtime 欄位。 |
| `r1_0005` | 6a | Model Gateway Hardening：`model_registry` formalize 成 `ModelEndpoint`（`protocol` / per-model `api_key_secret_ref` AES-GCM envelope / `classification_ceiling` / `supports_*`）；`health_status` 收斂為**五態**（`healthy` / `degraded` / `unhealthy` / `unknown` / `disabled`）。 |
| `r1_0006` | 7a | Service Registry：`platform_links` additive 升級為 `registered_services`（33 欄，保留原 id）+ `service_launches` · `service_audit_callbacks` · `service_project_bindings`；`service_access_grants` 加 `service_id` FK。 |
| `r1_0007` | 8a | Artifact 契約四表：`artifacts` · `artifact_versions` · `export_records` · `artifact_jobs`（**持久化** Studio 五 pipeline job → 滿足「restart 不丟 job」；Studio 走 HTTP service token 回報，不直讀 CSP DB）。 |
| `r1_0008` | R-SEC | `registered_services.service_client_id` FK：把 audit-callback 綁定到「屬於該服務」的 Service Client；fail-closed / default-deny，未綁定服務一律拒收 callback（`403`）。 |

---

## 6. 安全不變量

- **四級分類單向閂鎖**：等級序 `無機密 < 營業秘密 < 密 < 機密`；effective level = 觀測到分類取 `max`，**絕不降級**（`policy.apply_classification` 寫 `ClassificationEvent`）。**解密（declassification）不是移除的路由，而是受治理的申請工作流**：`declassification_requests` + 主管核准（`classification_authority_assignments`），fail-closed 預設 `pending_supervisor`。
- **卡登 SSO**：CSPKI 自然人憑證卡走真 PKCS#7 / CMS 驗簽（SignerInfo 簽章 + 憑證鏈 + nonce 反 replay），非只解析。
- **JWT / JWKS**：RS256（access + refresh，`tv` token-version 撤銷 claim），`GET /.well-known/jwks.json` 公開驗章。Launch token 共用同一 RS256 keypair / `kid`，registered service 以 JWKS **本地**驗（`aud` / `iss` / `exp` / 簽章）；TTL 10 分、**絕不**內嵌模型金鑰或長效 user JWT。
- **CSRF**：cookie 認證的變更請求走 double-submit（`X-CSRF-Token`，constant-time 比對，`CsrfMiddleware`）。
- **RLS / `csp_app`**：runtime 用非特權 `csp_app` role（RLS 才會生效）；migration 才用升權 `csp` superuser（見 §7）。
- **SSRF url_guard 分域（Slice 6a，doc 04 §8）**：`anila_core.security.validate_outbound_url(url, endpoint_kind=...)` 把 http 旗標按 `model` / `agent` / `generic` 分域——model endpoint 預設拒 http，**由 `ANILA_ALLOW_HTTP_ENDPOINT=1` 明確放行（PLAN.md P0.2，2026-07-29 拍板：production 與 dev 同準，內網模型 gateway 走 http）**；agent endpoint 由 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT` 放行（legacy `ANILA_ALLOW_HTTP_ENDPOINT` 仍作 fallback 但帶 deprecation 警告，供內網 MLSteam 純 http NodePort agent）。allow-list = `trusted_hosts` 表 + `ANILA_TRUSTED_HOSTS` env；`host.docker.internal` 為結構性拒絕，allow-list 解不開。
- **Credential 加密**：AES-256-GCM（`anila-core` `credential_crypto` / `service_token_envelope`；涵蓋 per-model `api_key_secret_ref`、`csk-` agent 憑證、ingestion 憑證）。
- **Token 撤銷**：持久 `token_revocations` 表 + JWT `tv` 強制 + Redis fan-out；`/api/auth/revocations` cold-start sync。
- **startup_security**：prod 對 `SECRET_KEY` / `ADMIN_PASSWORD` / `CSP_SERVICE_TOKEN` / DB 密碼等 dev 預設值拒絕啟動（空 `SECRET_KEY` 永遠 fatal；`ANILA_ALLOW_DEV_SECRET=1` 降為 warn）。入站另有 CORS allowlist（無 `*` fallback）、選用 TrustedHostMiddleware、SPA 路徑遍歷防護、nginx 安全 header + rate-limit。

---

## 7. 測試

測試自帶 sqlite（`tests/conftest.py` 把 `DATABASE_URL` 指到 per-session 的臨時檔，session 結束即刪，不碰 Postgres / 執行中容器），不需要先 export 任何環境變數：

```bash
python -m pytest services/csp/tests -q   # 從 repo 根目錄
cd services/csp && python -m pytest -q   # 或從這裡；兩者結果必須一致
```

**目前基準線（2026-07-31 實跑）**：**1 failed · 1296 passed · 13 skipped · 0 errors**（約 8 分鐘）。唯一的紅燈是 `test_template_download.py::test_developer_can_download_template`，那是 **production 真缺陷**（template 目錄解析回 404），不是測試問題。

📌 完整說明看 **[`tests/README.md`](tests/README.md)** —— 包含為什麼舊的「26 failing」基準線是假的（同一份碼從不同目錄跑會得到 27 vs 14 兩個答案）、執行順序污染的成因與修法、以及卡登測試的兩層結構。

---

## 8. Alembic 注意事項

- **`r1_` 命名空間**：redesign migration 以 `r1_` 前綴、線性接在 legacy 數字鏈之後（`r1_0001` `Revises: 0046`）。新增 module / 表時同步補 `.importlinter` 契約與 `app/schemas/contracts/`。
- **`MIGRATION_DATABASE_URL`（升權，僅 alembic 讀）**：migration 需 superuser 級連線（`0014` 要 `CREATE EXTENSION` / `CREATE ROLE csp_app`）。runtime `DATABASE_URL` 指非特權 `csp_app`（RLS 才會 fire）；`MIGRATION_DATABASE_URL` 是 alembic 專用的升權替身，未設時退回 `DATABASE_URL`（`migrations/env.py`）。compose 兩者拆開：runtime `csp_app:...`、migration `csp:...`。
- **啟動自動升級**：`app/main.py` lifespan 以 `command.upgrade(cfg, "head")` 程式化跑 `alembic upgrade head`。空庫也走 alembic（`MIGRATION_DATABASE_URL`／superuser）；失敗則拒絕啟動，不再 fallback `create_all`。`ANILA_SKIP_STARTUP_MIGRATIONS=1`（compose 預設 `0`）＝起服務但不遷移。

---

## 9. 啟動與部署

整合 stack（含 redis / ingestion-worker / router / anila-studio / 前端 / nginx）由根目錄 compose shim 定義：`compose.yaml` → [`infra/compose/platform.yml`](../../infra/compose/platform.yml)（prod，project `anila`）、`compose.dev.yaml` → `infra/compose/dev.yml`（dev）。

```bash
# 從 repo 根目錄
docker compose -f compose.dev.yaml up -d --build csp    # dev
docker compose up -d csp                                 # prod（platform.yml）
# 日常 lifecycle：infra/deployment/scripts/deploy-prod.sh
# 內網卡登 bootstrap：infra/deployment/intranet/intranet-deploy.sh
```

CSP 連兩個 network：`default`（stack 內部）與 `anila-models-net`（external，打 `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`）。第一次啟動若不存在：`docker network create anila-models-net`。

後端本地（不經容器、需自備 PostgreSQL）：`cd services/csp && .venv/bin/python -m uvicorn app.main:app --port 8000`。關鍵環境變數（`app/config.py` / compose）：`DATABASE_URL`（runtime `csp_app`）、`MIGRATION_DATABASE_URL`（升權）、`SECRET_KEY`、`JWT_KID`、`ADMIN_PASSWORD`、`ANILA_AUTH_MODE`、`CSP_SERVICE_TOKEN`、`MODEL_GATEWAY_API_KEY`、`ANILA_ENV`（部署姿態；自 PLAN.md P0.2 起不再影響 model http 判定）、`ANILA_ALLOW_HTTP_ENDPOINT` / `ANILA_ALLOW_HTTP_AGENT_ENDPOINT` / `ANILA_ALLOW_PRIVATE_ENDPOINT`、`ANILA_TRUSTED_HOSTS`、`REDIS_URL`、`ENABLE_PUBLIC_SHARE`。JWT PEM 路徑固定為 `secrets/jwt-{private,public}.pem`。詳見 [`.env.example`](./.env.example)。

---

## 10. 相關文件

- 設計沿革（收斂紀錄）：[`docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) — 憲章 [`00`](../../docs/anila-redesign-docs/00-product-constitution.md)、CSP 治理控制面 [`03`](../../docs/anila-redesign-docs/03-csp-governance-control-plane.md)、Model Gateway [`04`](../../docs/anila-redesign-docs/04-model-gateway-design.md)、Agent Registry [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md)、Service Platform [`07`](../../docs/anila-redesign-docs/07-registered-gui-service-platform.md)、分類閂鎖與政策引擎 [`08`](../../docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md)、API / 事件契約 [`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md)、遷移與開發護欄 [`10`](../../docs/anila-redesign-docs/10-migration-and-development-guardrails.md)。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。
- 平台整體：[`../../README.md`](../../README.md)。
- 模組邊界契約：[`.importlinter`](./.importlinter)（`infra/ci/lint-boundaries.sh`）。

---

**Role**：Control + Data Plane · **Authoritative for**：users · api_keys · models · agents · service_clients · **tasks · trace_spans · policy_decisions · classification** · registered_services · artifacts · token_usage · audit_logs · ingestion 知識庫（Studio + FLUX + 圖表渲染已抽到 anila-studio，CSP 保留 contract endpoint 與持久化 artifact job store）
