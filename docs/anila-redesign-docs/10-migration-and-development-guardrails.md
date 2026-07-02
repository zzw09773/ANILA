# 10. Migration Plan and Development Guardrails

> Status: draft v0.1  
> Purpose: 定義從 `prod-intranet-card` 到新專案的遷移順序、保留策略、避免重工策略、CI/PR guardrails。  
> Principle: 先保留成熟骨架，再升級 contract；不要以「新專案」為名重寫所有已驗證的安全與 runtime 能力。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. Migration Principles

1. 保留 card SSO / JWT / JWKS / revocation / CSRF / RLS。
2. 保留 CSP Control + Data Plane 作為 authoritative store。
3. 保留 OpenAI-compatible `/v1/*` surface。
4. 保留 `model_registry`、`agents`、`platform_links`、`service_access_grants` 作為 migration seed。
5. 先補 Task / Trace / Classification contract，再動大 schema。
6. OpenWebUI 不遷入架構，只遷移它上面的 Agent metadata / endpoint。
7. Studio 不重寫；先統一 artifact contract。
8. 一切正式 Agent 以 Full Trace 作為準入條件。

---

## 2. Slice 0: Documentation Baseline

交付：

- 11 份 design docs。
- repo evidence map。
- ADR template。
- PR checklist。
- Codex deep audit prompt。

Done:

- 新 repo `/docs` 可直接放入。
- 11 份 design docs 均已補入 repo evidence / 現況補齊段落，並移除原本的專案審查清單。
- 所有已確認決策進 ADR。

---

## 3. Slice 1: CSP Skeleton Lift

目標：把成熟 CSP 骨架移到新專案。

Keep:

- FastAPI app structure。
- RS256 JWT/JWKS。
- card auth fork。
- API key validation。
- model registry。
- agents table。
- proxy service。
- token_usage。
- audit_logs。
- service_clients。
- platform_links / grants。
- ingestion API skeleton。

Refactor only:

- module layout。
- naming。
- contract schemas。
- tests organization。

Do not:

- 改 card auth。
- 改 RLS migration 主要策略。
- 改 proxy 成另一套 gateway。

---

## 4. Slice 2: Task Service and Source Snapshot

新增：

```text
tasks
task_runs
source_snapshots
citations
policy_decisions
trace_spans
```

先在 CSP 內部實作，不急著拆獨立服務。

> ✅ MVP 決策（2026-07-02 拍板，與 doc 02 §1 一致）：Task Service、Policy
> Engine、Service Launch Gateway 在 MVP 階段一律實作在 CSP service 內，
> 以 module boundary 隔離（獨立 package／router，禁止跨模組直接 import
> 內部實作）；v1.1 後再評估抽 service。

Done:

- ANILA UI 建立 Task。
- 每次 `/v1/chat/completions` 可帶 `task_id`。
- 無 task_id 的舊流量仍相容，但標記 `legacy_runtime_call`。

---

## 5. Slice 3: Classification Upgrade

新增五級欄位：

```text
classification_level
classification_event
declassification_request
supervisor_approval
```

Backfill:

```text
classified=false -> 無機密
classified=true  -> 機密
```

Done:

- 舊 boolean latch 不破。
- 新 UI 用 level badge。
- Admin 降級申請 + supervisor approval 可跑通。
- Model/Agent/Service policy 會檢查 level ceiling。

---

## 6. Slice 4: Full Trace Foundation

> ⛔ **P0 Full Trace Implementation Gap**（與 doc 05 §13、doc 09 §10 同一 P0，勿低估）：
>
> - `/v1/traces/{trace_id}/spans` 尚未存在（repo 內無任何 trace 收取端點）。
> - `trace_spans` table 尚未存在（現只有 `InMemoryProcessor`，spans 不落地）。
> - `anila_trace_sdk` 尚未存在。
> - anila-agent `service_wrapper` 目前刻意不外送 tool / reasoning trace。
> - anila-core tracing hooks 尚未接入 Router dispatch 與 Agent runtime。
> - UI 已有 SpanTree 元件與 `anila.spans` 接線 ≠ Full Trace 已完成——該事件現無 producer（RESERVED）。
> - 正式 Agent approval 必須 blocked until trace-test passed。

新增：

```text
trace_spans
/v1/traces/{trace_id}/spans
Trace Explorer UI
anila_trace_sdk
```

Done:

- Router / proxy / model call 至少產生 spans。
- anila-agent sample 產生 full trace。
- ANILA UI 可顯示 SpanTree。
- 無 full trace Agent 不能 approved 正式使用。

---

## 7. Slice 5: Agent Registry Migration

> ✅ 已拍板（2026-07-02，與 doc 06 對齊）：OpenWebUI 自動化匯出匯入、
> sidecar、Pipe bridge 一律**不列入 v1 交付項**；遷移走人工盤點＋
> 既有精靈 / CLI 手動重新註冊。

新增：

- Agent runtime_type。
- manifest schema。
- trace-test endpoint。
- 人工盤點 inventory（CSV / 表單，取代自動匯入）。
- CSP shadow registration flow。
- 現有 wizard / CLI 註冊流程強化。
- anila-agent native Full Trace sample。
- LangChain / custom HTTP trace adapter sample。

不做（✅ 已拍板 2026-07-02，見 doc 06 文首）：

- OpenWebUI import wizard。
- `anila agent import-openwebui` CLI。
- OpenWebUI Pipe 自動 bridge / sidecar 自動包裝。

Done:

- 現有 ML Team agent 完成人工盤點並 shadow register。
- 至少 1 個 anila-agent 完成 native trace。
- 至少 1 個非 anila-agent runtime（LangChain / custom HTTP）以 trace adapter 完成 Full Trace。
- Router 只從 CSP `/v1/agents` discovery。

---

## 8. Slice 6: Model Gateway Hardening

新增：

- per-model secret ref。
- classification ceiling。
- model health。
- policy decision before invocation。

Done:

- 所有模型都是院內 HTTPS + API Key endpoint。
- Agent / Router 不持模型 key。
- usage 和 trace 都能歸戶。

---

## 9. Slice 7: Project Entry / Service Registry

基於 platform links 擴充：

```text
registered_services
service_launches
service_audit_callbacks
service_project_bindings
```

Done:

- 其他小組 Admin 可上架服務。
- iframe 可從 ANILA 啟動。
- launch token 可驗證。
- audit callback 可寫入。
- access grants default deny。

---

## 10. Slice 8: Studio Artifact Contract

新增：

```text
artifacts
artifact_versions
artifact_jobs
export_records
```

Refactor:

- 五類 pipeline 用共同 job model。
- artifact persistence 不再混用 memory/disk。
- artifact 繼承 classification。
- artifact 指向 task/source_snapshot。

Done:

- 報告、簡報、心智圖、資訊圖、資料表都能回 trace。
- crash/restart 不丟 job metadata。

---

## 11. Slice 9: ANILA Shell IA

導覽：

```text
任務中心
我的知識庫
產出中心
專案入口
```

Admin 顯示：

```text
治理中心
```

Done:

- 不顯示 ANILALM / Studio / CSP 作獨立產品。
- Project Entry 服務卡片從 registry 讀取。
- Task result 可轉 artifact 或 launch service。

---

## 12. Code-level Guardrails

### Import Rules

```text
apps/anila-shell 不可 import model client
apps/anila-shell 不可 import agent endpoint client
runtime 不可 import UI
agents 不可 import CSP database models
studio 不可直接讀 CSP DB
```

### Runtime Rules

```text
所有正式 model call 必須通過 CSP proxy
所有正式 agent call 必須通過 CSP proxy
所有正式 service launch 必須通過 Launch Gateway
所有正式 artifact 必須有 task_id 或 source_snapshot_id
所有正式 task 必須有 trace_id
```

### Classification Rules

```text
不得直接降低 classification_level
不得複製高分類內容到低分類 artifact
不得用低 ceiling model/agent/service 處理高分類 task
```

---

## 13. PR Checklist

```md
## 收斂檢查

- [ ] 這個變更屬於：任務中心 / 我的知識庫 / 產出中心 / 專案入口 / 治理中心
- [ ] 沒有新增未核准一級入口或產品名稱
- [ ] 若涉及模型，已走 Model Registry + CSP Proxy
- [ ] 若涉及 Agent，已走 Agent Registry + Full Trace
- [ ] 若涉及 GUI Service，已走 Service Registry + Launch Contract
- [ ] 若涉及知識來源，已建立 SourceSnapshot / Citation
- [ ] 若涉及 Artifact，已綁定 Task / SourceSnapshot
- [ ] 若涉及分類，已處理 classification propagation
- [ ] 若涉及降級，已走 Admin request + supervisor approval
- [ ] 有 PolicyDecision / AuditEvent / TraceSpan
- [ ] 不繞過 card SSO / JWT / CSRF / RLS / SSRF guard
- [ ] 有 contract test 或 migration test
- [ ] 若違反任一項，已附 ADR
```

---

## 14. CI Gates

```text
lint-boundaries
schema-contract-tests
openapi-diff
sse-contract-tests
trace-contract-tests
classification-policy-tests
migration-up-down-tests
security-smoke-tests
```

---

## 15. Testing Matrix

| 類型 | 測試 |
|---|---|
| Unit | classification max, access control, manifest validation |
| Contract | `/v1/chat`, SSE events, trace spans, service launch |
| Integration | Task → Router → Agent → Model → Trace |
| Security | SSRF, auth, API key, service token, RLS |
| Migration | classified boolean backfill, agents migration |
| E2E | user card login → task → artifact → audit |
| GUI Service | launch token → iframe → audit callback |
| Agent | Full Trace required span completeness |

---

## 16. Backlog Priority

| P | 說明 |
|---|---|
| P0 | Task / trace / classification / CSP proxy 不可缺 |
| P1 | Agent migration / Model gateway / Service Registry |
| P2 | Studio artifact persistence |
| P3 | UX polish / prompt library / chat export / banners |
| Park | marketplace、swarm、自組 workflow builder |
| Reject | 無 trace 黑盒、繞過 CSP、直連模型 |

---

## 17. Repo Evidence / 現況補齊

本節證據最初取自 main 形狀的工作樹。注意：本機名為 `prod-intranet-card` 的分支
已與遠端分歧（相對 `origin/prod-intranet-card` ahead 22 / behind 92），且不含任何
card 產物（無 card 測試檔、無 intranet 部署腳本）。遷移來源的權威基準
（source of record）是 **`origin/prod-intranet-card`（v1.2.0 系）**；現況描述以該
ref 為準（唯讀 `git show origin/prod-intranet-card:...` 取證），本機工作樹與其
分歧時以 origin 為準。新專案若以 `prod-intranet-card` 為遷移來源，card 分支的
auth / migration / deploy 差異必須優先於 `main`。

Card 分支的 compose delta 不只 auth / migrations / deploy script，還包含
（皆已用 `git show origin/prod-intranet-card:docker-compose.yml` 驗證）：

- `csp` 環境變數 `SSL_CERT_FILE: ${ANILA_MODEL_CA_FILE-}`（~:144）：出向模型
  gateway 的 CA 信任。footgun：`SSL_CERT_FILE` 是「取代」整個系統信任庫而非
  疊加，指到空 / 壞檔會讓 csp 所有出向 https 全部驗證失敗。
- `./share/pki:/etc/anila/pki:ro`（~:165）：內網 CA PEM 的 drop-dir。
- `./secrets:/app/secrets:ro`（~:170）：RS256 JWT keypair 掛載；prod 模式不自動
  產 key，由 `intranet-deploy.sh` [4b] 產到 `./secrets`。
- 遠端模型 gateway 以 FQDN `aiagent2.ai.ncsist.org.tw` 呼叫；origin compose 的
  `extra_hosts` 只有 `host.docker.internal:host-gateway`，FQDN 解析依
  `intranet-deploy.sh` 收尾提示交由 DNS（或部署現場自行補 hosts 對應）——這是
  遷移時應顯式化的缺口。

### 17.1 實際可搬移檔案與新 repo path mapping

| 現 repo path | 建議新 repo path | 搬移策略 / repo evidence |
|---|---|---|
| `myCSPPlatform/backend/app/`, `myCSPPlatform/backend/alembic.ini`, `myCSPPlatform/backend/migrations/`, `myCSPPlatform/backend/requirements.txt` | `services/csp/` | 直接 lift 作 CSP Control + Data Plane。`app/main.py` 目前在 lifespan 內跑 Alembic upgrade、`startup_migrations`、auto-seed、trusted-host backfill、health/usage background tasks；不可只搬 API router。 |
| `myCSPPlatform/frontend/` | `apps/csp-governance-ui/` | 作 Admin / Developer / Service Admin 治理 UI。`package.json` 只有 `dev/build/preview`，沒有測試 script。 |
| `myCSPPlatform/docker/Dockerfile` | `infra/docker/csp.Dockerfile` 或 `services/csp/Dockerfile` | 正式 CSP image 來源；compose 用 root build context 並 COPY `anila-core`、CSP backend、CSP frontend build。`myCSPPlatform/backend/Dockerfile` 不應當部署來源。 |
| `myCSPPlatform/docker/nginx.conf` | `infra/nginx/anila.conf` | 可移植為唯一外部入口設定。現況含 `443` CSP / `/api` / `/v1` / `/router` / `/anila/`（同源 ANILA runtime UI 主要入口，見 doc 02 §10）/ `/anilalm` / `/n8n` / `/gitlab` / `/codeserver`，`4443` 為 runtime UI 的 legacy / 替代入口，Studio artifact regex route 到 `anila-studio:8100`，且有 rate limit / security headers。 |
| `myCSPPlatform/docker/certs/` | 不直接搬私鑰；只搬 sample / 產生腳本 | 現有 nginx 掛 certs；新 repo 不應 commit 真憑證或 private key。 |
| `anila-core/` | `packages/anila-core/` | Runtime SDK。Router、CSP inspector、ingestion-worker 都會安裝它；`pyproject.toml` 含 `dev`、`rag` extras 與 integration marker。 |
| `anila-core-router/` | `services/anila-core-router/` | 部署 wrapper。`main.py` 只組裝 `anila_core.api.router_server.create_router_app()`，並處理 Router service token state file / bootstrap / legacy env。 |
| `anila-agent/` | `packages/anila-agent/` 或 `templates/anila-agent/` | 官方 agent starter/template。root compose 把它唯讀掛到 CSP 的 `/app/anila-template`；搬家後要同步 `ANILA_TEMPLATE_DIR` 與 template download endpoint。 |
| `ingestion-worker/` | `services/ingestion-worker/` | Arq worker，無 HTTP port。`Dockerfile` 從 root context 安裝 `anila-core[rag]` 與 worker；系統依賴 `antiword` 是 `.doc` parser 必要條件。 |
| `anila-studio/` | `services/anila-studio/` | FastAPI artifact service。`app/main.py` 啟動時拉 CSP JWKS 與 Redis revocation cache，ready 前 `/health` 回 503；不共用 CSP DB。`openapi/studio.openapi.json` 與 `scripts/export-openapi.py` 要一起搬。 |
| `ANILA_UI/anila-ui/` | `apps/anila-shell/` | Runtime chat UI 的可重用來源；新產品不應保留 `ANILA_UI` 品牌作平行產品。`package.json` 有 `test` / `build`，Vitest 測 SSE、classified、SpanTree、tool execution 等。 |
| `ANILALM/` | `apps/anila-shell/features/knowledge/` 與 `apps/anila-shell/features/artifacts/` | 知識庫與 Studio 前端功能可併入 ANILA Shell。`package.json` 有 `typecheck` / `build` / `gen:studio-types`，沒有 test script。 |
| `ANILALM/pptx-skill/` | `services/pptx-renderer/` | 現在是 compose service `pptx-renderer:7100`，由 `anila-studio` server-to-server 呼叫；`package.json` 沒 scripts，測試需手動 `node tests/test_*.js`。 |
| `models/docker-compose.yml` | `infra/models/docker-compose.yml` | 可搬 compose topology，但不能搬成固定 host path。現況使用 absolute model volume path、GPU `device_ids`、external network `anila-models-net`，所有模型只 `expose` 不開 host port。 |
| `models/flux2-dev/`, `models/flux2-dev-agent/` | `services/flux2-dev/`, `services/flux2-dev-agent/` 或 `infra/models/services/` | 有可重用程式與 pytest。`flux2-dev-agent` 是 image-generator agent shim，現況無外部 auth，必須只放 CSP / 內網後面。 |
| `docker-compose.yml`, `docker-compose-dev.yml` | `infra/compose/platform.yml`, `infra/compose/dev.yml` | root stack `name: anila-platform`，dev stack `name: anila-platform-dev`，dev stack 使用獨立 ports / volumes / network，兩者共用 `anila-models-net`。 |
| `scripts/deploy-prod.sh`, `scripts/reissue-tls-cert.sh`, `scripts/reencrypt-credentials.py`, `scripts/diagnose-graphviz.sh` | `infra/deployment/scripts/` | 可移植。`scripts/phase1-e2e.sh` 仍測 `/codeserver/`，對精簡 prod 不應當成新 repo E2E baseline。 |
| `scripts/intranet-deploy.sh`, `scripts/build-and-export-for-intranet.sh`, `scripts/download-intranet-models.sh`, `scripts/download-intranet-toolkit.sh`, `scripts/model-serve.sh`, `scripts/anila-serve.sh`, `scripts/pack-chunks.sh` / `scripts/unpack-chunks.sh`, `scripts/intranet-quantize-nvfp4.py`, `docs/runbooks/intranet-deployment-runbook.md` | `infra/deployment/intranet/` 與 `docs/runbooks/` | 內網 / air-gap 部署工具鏈（皆在 `origin/prod-intranet-card`，以 `git ls-tree` 驗證）。`intranet-deploy.sh` 是 card 一次性 bootstrap：從 `server.pfx` 抽 TLS 憑證、產 secrets、組 `.env`、接 CSPKI model-CA（`cspki_ca_bundle.pem`）、[4b] 產 JWT keypair，收尾交棒 `deploy-prod.sh` 做日常 lifecycle。`START-HERE.sh` 只存在於離線 bundle，從未進 repo。 |

### 17.2 目前 tests 覆蓋與可重用測試

現況沒有 repo-wide coverage gate，也沒有可信的整體覆蓋率數字；不要在 migration
plan 宣稱 80%+ 已達成。可重用的是測試入口與高價值 test files。

| 範圍 | 現有測試入口 | 高價值可重用 test files |
|---|---|---|
| `myCSPPlatform/backend` | `cd myCSPPlatform/backend && python -m pytest` | `tests/test_cookie_auth.py`, `test_jwks_endpoint.py`, `test_rs256_jwt.py`, `test_revocations_endpoint.py`, `test_startup_security.py`, `test_models_ssrf.py`, `test_ssrf_call_time_guard.py`, `test_url_guard_opt_in.py`, `test_url_guard_providers.py`, `test_proxy_stream_usage.py`, `test_proxy_classified.py`, `test_agent_registration.py`, `test_agent_credentials.py`, `test_trusted_host_service.py`, `test_ingestion_images_rls_pg.py` |
| `prod-intranet-card` CSP auth fork | 同 backend pytest，但要在 card branch 跑 | `origin/prod-intranet-card` 含 `tests/test_card_auth.py`, `test_card_endpoints.py`, `test_intranet_lockdown.py`, `test_gateway_auth.py`, `test_employee_id_downstream.py`；這些在目前 `main` 不是原始測試檔，只剩部分 stale `__pycache__`。 |
| `anila-core` | `cd anila-core && pip install -e '.[dev,rag]' && pytest`; DB/RLS 類另跑 `pytest -m integration`; 品質跑 `ruff check src tests`, `mypy src` | Router / session / SSE：`tests/test_router.py`, `test_router_sse_passthrough.py`, `test_router_resume_proxy.py`, `test_router_session.py`, `test_router_multi_turn.py`, `test_router_streaming_multi_turn.py`, `test_router_classified.py`; RLS：`tests/integration/test_g1_collection_isolation.py`, `test_g2_rls_bypass.py`; ingestion SDK：`test_chunking_plugins.py`, `test_collection_scoped_pgvector_store.py`, `test_citation_extractor.py` |
| `ingestion-worker` | `cd ingestion-worker && pip install -e '../anila-core[rag]' -e '.[dev]' && pytest && ruff check src tests` | `tests/test_embedder.py`, `test_parsers.py`, `test_handlers_helpers.py`, `test_judge.py`, `test_llm_relations.py`, `test_evaluator_metrics.py`, `test_settings.py` |
| `anila-agent` | `cd anila-agent && make install && make test && make lint`; live endpoint 才跑 `make test-live` | `tests/test_service_wrapper.py`, `test_service_auth.py`, `test_streaming.py`, `test_csp_http_retriever.py`, `test_model_airgap.py`, `test_anila_pgvector.py`, `test_postgres_runstate.py`, `test_policy.py`, `test_live_smoke.py` |
| `anila-studio` | `cd anila-studio && pip install -e '.[dev]' && pytest` | Auth / deps：`tests/test_app_lifespan.py`, `test_auth.py`, `test_jwks_client.py`, `test_revocation_cache.py`, `test_csp_client.py`; artifact endpoints/renderers：`test_report_endpoint.py`, `test_mindmap_endpoint.py`, `test_infographic_endpoint.py`, `test_datatable_endpoint.py`, `test_phase3_wiring_smoke.py`, `test_studio_flux_e2e.py` |
| `ANILA_UI/anila-ui` | `cd ANILA_UI/anila-ui && npm test && npm run build` | `src/__tests__/sse.test.js`, `spanTree.test.jsx`, `classified.test.js`, `classifyRetryQueue.test.js`, `toolExecution.test.jsx`, `agentic.test.jsx`, `normalizeAgents.test.js`; `e2e/README.md` 不是可靠 Playwright spec。 |
| `ANILALM` | `cd ANILALM && npm run typecheck && npm run build`; schema 改動先 `npm run gen:studio-types` | 無 test script；用 typecheck/build 作最低 gate。 |
| `ANILALM/pptx-skill` | 無 npm scripts；手動 `node tests/test_cover_hero_guard.js` 等 | `tests/test_cover_hero_guard.js`, `test_hierarchy_bullets.js`, `test_image_focus_render.js`, `test_local_emptiness.js` |
| `models/flux2-dev` | `cd models/flux2-dev && pip install -e '.[test]' && pytest` | `tests/test_server.py`，測試用 mock pipeline，不載大型權重。 |
| `models/flux2-dev-agent` | `cd models/flux2-dev-agent && pip install -e '.[test]' && pytest` | `tests/test_chat_handler.py`, `test_flux_client.py`, `test_image_store.py`, `test_prompt_translator.py`, `test_schemas.py`, `test_main.py` |

補充：目前可見 Python test files 約 172 個（排除 `__pycache__`），但沒有統一
coverage report。新 repo 應建立 coverage baseline，再把上表測試納入 CI matrix。

### 17.3 Alembic migrations 與 squash baseline 判斷

現況：

- Alembic 只在 `myCSPPlatform/backend/migrations/`。目前 `main` tracked migration
  files 是 45 個，revision 從 `0001` 到 `0046`，刻意沒有 `0025`；`0026` 的
  `down_revision` 直接接 `0024`，註解說舊 `0025_add_action_functions` 已移除。
- `main` 的 `0035` 是 `0035_drop_sso_and_local_password_disabled.py`；但
  `origin/prod-intranet-card` 的 `0035` 是 `0035_iso_42001_traceability.py`，
  且保留 `auth_providers` / `external_identities` / card auth schema。從
  `prod-intranet-card` 遷移時不能拿 `main` 的 `0035` baseline 覆蓋。
- `migrations/env.py` 會優先讀 `MIGRATION_DATABASE_URL`；這是 migration
  superuser connection，用於 `CREATE EXTENSION vector`、`CREATE ROLE csp_app`
  與 RLS/ownership DDL。runtime `DATABASE_URL` 應維持 `csp_app`。
- `app/main.py` startup 順序是：`assert_no_dev_defaults()` → Alembic
  `upgrade head` → fallback `Base.metadata.create_all`（只應視為測試/救援）
  → `run_startup_migrations()` → auto-seed / trusted-host backfill。
- `app/services/startup_migrations.py` 仍存在且做兩類事：早期 baseline 欄位 /
  index backfill，以及 `LEGACY_SQLITE_PATH` SQLite → Postgres 遷移。這代表 schema
  真相不只在 Alembic versions；若新 repo 建 squash baseline，要把這些 backfill
  吸進 baseline，或保留 legacy startup migration 到確認所有舊 DB 已轉完。
- 高風險 DDL 已存在：`0014` 建 pgvector / `csp_app` / RLS 基礎與 ingestion
  tables；`0015` 將 chunks 改 `halfvec(4000)`；`0026` 建 `ingestion_images`
  `halfvec(4000)`；`0037` 對 ingestion images 啟用 FORCE RLS 並建立
  `SECURITY DEFINER` resolver；`0040` 是 auth provider schema guard；
  `0046` 修 `document_relations` composite FK cascade。

結論：

- 新 repo 可以建立新的 `0001_baseline`，但只適合「乾淨新 DB」；baseline
  必須以 `prod-intranet-card` schema 為來源，保留 card/SSO、RS256/JWKS、
  revocation、RLS、pgvector/halfvec、`csp_app` role、`service_clients`、
  `agent_credentials`、ISO traceability 等。
- 不建議在既有 ANILA repo 或 downstream branch 直接 squash / rewrite
  migration history。原因是 live DB 的 `alembic_version`、`startup_migrations`
  backfill、以及 `main` / `prod-intranet-card` 的 `0035` 分歧都會讓操作風險過高。
- 若要支援舊 production DB 進新 repo，應保留 legacy migration path：
  舊 chain 升到原 head → 資料驗證 → stamp 到新 baseline，或寫明確
  data migration，不應讓 `create_all` fallback 成為正式路徑。

### 17.4 `scripts/deploy-prod.sh` 可否移植

可移植，但應搬 `prod-intranet-card` 版本作基礎，不只搬目前 `main` 的版本。
理由：

- 兩個版本都有 subcommands：`preflight`, `deploy`, `up`, `down`, `restart`,
  `rebuild <svc>`, `status`, `logs <svc>`, `verify`, `wait`。
- 兩個版本都會檢查 prod branch 白名單、Docker daemon、docker compose v2、
  必要 env (`CSP_SERVICE_TOKEN`, `INTERNAL_PLATFORM_API_KEY`, `CSP_SECRET_KEY`
  或 `SECRET_KEY`)、`share/uploads/flux`、模型 stack / container health。
- `prod-intranet-card` 版本另支援 `ANILA_REMOTE_MODELS=1`，會改用
  `*_BASE_URL` 探測遠端模型，並建立空的 `anila-models-net` 以滿足 compose
  external network；也會建立 `share/pki`。
- `prod-intranet-card` 版本在 `deploy` / `up` 前會 `ensure_jwt_keypair()`，
  產生 `secrets/jwt-private.pem` / `jwt-public.pem` 並設定權限。這比 `main`
  版本更符合 RS256/JWKS production 需求。
- deploy path 用 `docker compose build` + `docker compose up -d`，不是
  `docker restart`，可正確套用 compose/env/build 改動。

移植前必改 / 補強：

- 分支白名單要改成新 repo 的 release branch / environment guard，不應硬綁
  `prod-intranet-card`, `prod-public-passwd`, `prod-military-passwd`。
- service 名稱、project name、compose 檔路徑、model container 名稱與
  absolute host model paths 都要參數化。
- `verify` 目前只 smoke：nginx `/health`、CSP internal `/health`、
  anila-studio `/health`、CSP `/api/auth/revocations`。新 repo CI/deploy
  還要補 card login、CSRF cookie、`/v1/chat/completions`、Router dispatch、
  ingestion upload → search、Studio artifact job。
- secret checks 仍需擴充：確認 `ANILA_ALLOW_DEV_SECRET` 未啟用、`ALLOW_AUTO_KEYGEN`
  production 不開、JWT keypair 存在且權限正確、`secrets/` 不被 git track、
  model gateway API key / CA bundle 設定可用。
- `scripts/phase1-e2e.sh` 仍含 `/codeserver/` 假設；不要隨 deploy script 一起
  當作新 prod 驗證標準。

### 17.5 分支模型與遷移紀律

現行 repo 是 7 分支模型（權威參考：repo 根 `AGENTS.md` §3）：

- `main` 是 SSOT：通用 feature / bugfix / docs / 測試先進 `main`，再扇出
  downstream；downstream 之間不互相 merge，跨分支修補一律先進 `main` 再分別 port。
- `prod-intranet-card` 是唯一 card/SSO fork；auth / card 檔案不得被 `main`
  wholesale merge 覆蓋。
- `trial-military` 是刪減型分支（已移除 dev-tooling views，如
  `DeveloperAgentsView.vue` / `DeveloperGuideView.vue`），只做挑選式 port，
  不做整批 merge。

從 card 分支抽遷移素材進新專案時，必須保留這套同步紀律：card-only delta
（auth、compose 掛載、intranet 工具鏈）不可誤當通用內容回流 `main` 或其他
分支；通用修補也不可只落在 card 分支而缺 `main` 對應。
