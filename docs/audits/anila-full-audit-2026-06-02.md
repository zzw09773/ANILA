# ANILA 專案全面審計報告

> **日期**:2026-06-02
> **方法**:9 階段多 agent workflow(176 agents、5.07M tokens、41 分鐘),六大面向各派獨立 lens,high/critical 走 3-lens 投票面板、medium/low 走 skeptic 對抗驗證。
> **規模**:120 條 raw findings → 88 條納入本報告(43 條走完驗證 + 45 條 transcript 救回)。
> **可信度標記**:✅ 已驗證 / ✅ 人工程式碼驗證(critical/high)/ ⚠️ 待複驗(救回的 medium/low)。

## 統計

| 面向 | 已驗證 | 救回待整理 |
|---|---|---|
| 資安 (security) | 10 | — |
| UIUX (ux) | 19 | — |
| 可改進 (improvements) | 14 | — |
| 新功能 (features) | — | 17 |
| Dev 體驗 (dev-experience) | — | 14(含 2 critical) |
| anila-agent 樣板 | — | 14 |

嚴重度(已驗證部分):critical 0、high 6、medium 16、low 18、info 3。
救回部分另含 **2 條 critical**(均為 dev-experience,已由人工程式碼驗證)。

---

## 🔴 最關鍵更正:militarylaw-agent 既有架構錯誤(已用程式碼證實)

審計直接抓到先前 `militarylaw-agent/BUILDING_THIS_AGENT.md` 的核心錯誤,**此條經人工逐行程式碼驗證為真**:

| 先前文件所述(錯誤) | 程式碼實際行為(正確) |
|---|---|
| 「csp Router dispatch 會 propagate 使用者 JWT,agent 用它打 search」 | `proxy_service.py:136-144`:Router dispatch 給 agent 時送 `X-CSP-Service-Token` + `X-ANILA-User-Id/Email/Groups`,**完全不帶 user JWT** |
| 「agent 取 caller bearer 打 search API」 | `search.py:242` 使用 `get_current_user` → **只接受 JWT/cookie,拒絕 service token** |

**後果**:militarylaw-agent 的 `service.py:_extract_bearer` 找 `Authorization: Bearer`,被 Router dispatch 時直接 401;即使改讀 `X-ANILA-User-*` 也無 JWT 可回打 search,而 search 又不收 service token。

**anila-studio 為何能運作**:studio 是被前端 SPA **直接呼叫**(瀏覽器帶 user JWT),`auth.py:203 get_bearer_token` 取出 JWT 再轉發 csp(convention #2)。Router dispatch 是 convention #1(service token + user headers)。兩者不同,先前文件把它們混為一談。

**衍生平台 gap(待決策)**:目前 Router-dispatched agent **沒有乾淨方式**做 user-scoped RAG 檢索。候選方案:
1. **csp 開 service-token 認證的 search 端點**:用 `X-CSP-Service-Token` 認證 + `X-ANILA-User-Id` 做 RLS scope。改動在 csp,agent 端最單純。
2. **Router propagate 短期 user JWT**:改 `proxy_service` dispatch 時帶短期 JWT。改動在 Router,但需處理 JWT 生命週期/撤銷。

> 此決策尚未拍板(user 要再評估整體認證架構),記錄於此待後續。

---

## 優先處理建議

**立即(資安/阻斷)**
1. `.env:18` 移除 `ANILA_ALLOW_DEV_SECRET` 或設 0,並設強 `ADMIN_PASSWORD`(見 S1)。
2. 修 ingestion audit log 不落地(見 S2)—— 對中科院內/國軍合規為硬需求。

**高(解開 dev onboarding + 修正錯誤交付)**
3. 平台拍板上方 RAG 認證 gap。
4. 重寫 militarylaw-agent `BUILDING_THIS_AGENT.md` 為真實 dispatch 協議。
5. anila-agent 加 `build_agent(retriever=...)` escape hatch(見 H1/H2)。

**中(樣板加速,一次到位)**
6. csp_http retriever + service wrapper + Makefile + `anila init` + dockerized hello-world 補進樣板。

---
## 附錄 A:子系統地圖

### csp — `/home/aia/c1147259/ANILA/myCSPPlatform/`

myCSPPlatform is a FastAPI "control + data plane" gateway for the ANILA platform: it manages users/models/agents/API-keys (control plane via JWT cookies/headers) and proxies OpenAI-style /v1/* inference traffic plus document ingestion/semantic search (data plane via either JWT or sk- API keys). Auth uses asymmetric RS256 JWTs (CSP signs with a private PEM, downstreams verify via /.well-known/jwks.json), while data-plane SDK callers present sk- bearer keys; both resolve to the same User via the get_caller dependency. Tenant isolation for ingested document_chunks is enforced at the Postgres engine with Row-Level Security keyed on the anila.collection_id GUC, run under a non-privileged csp_app role, with migrations applied via Alembic under an escalated superuser connection.

**技術棧**:Python, FastAPI, SQLAlchemy (sync ORM), asyncpg + anila_core PgPool (async data-plane), Alembic migrations, PostgreSQL 16 with pgvector/halfvec + Row-Level Security, python-jose RS256 JWT + JWKS, passlib/bcrypt, Pydantic / pydantic-settings, httpx (upstream proxy), Vue 3 + Vite + Tailwind (frontend SPA), Docker Compose + nginx reverse proxy

**關鍵檔案**:
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/main.py : FastAPI app entrypoint — lifespan runs Alembic upgrade, startup-security guard, auto-seed, opens async ingestion pool; mounts CORS + CSRF middleware, routers, SPA static serving`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/config.py : Pydantic settings — DATABASE_URL (csp_app runtime role), RS256 JWT key paths/KID, ALLOWED_ORIGINS, COOKIE_SECURE, CSP_SERVICE_TOKEN, dev-default secrets`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/utils/security.py : RS256 JWT sign/verify (private/public PEM loading, kid pinning, explicit algorithms allowlist to block alg-confusion) + bcrypt password hashing`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/services/auth_service.py : Control-plane auth — get_current_user (header>cookie JWT), token_version revocation check, require_admin/require_owner role gates, verify_service_token for X-CSP-Service-Token`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/middleware/caller.py : Data-plane unified Caller dependency for /v1/* — discriminates sk- API keys vs JWT, resolves both to a single User identity + api_key_id for usage attribution`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/services/api_key_service.py : sk- API key minting (sha256 hash storage, prefix/suffix), validate_api_key (active + user.is_active gate), per-key model/agent permission checks`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/api/router.py : Aggregates all control-plane (/api/*) and data-plane (/v1/*, JWKS) routers into one APIRouter mounted by main`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/api/proxy.py : Data-plane inference gateway — /v1/chat/completions, /v1/embeddings, /v1/models, /v1/agents (model_registry endpoint resolution + retry/usage metering)`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/api/ingestion/collections.py : Collections CRUD + _require_collection_access authz helper (admin-tier bypass else created_by owner) reused by search/documents`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/api/ingestion/search.py : Semantic top-K + image search — embeds query via proxy then queries CollectionScopedPgVectorStore over the RLS-scoped async pool`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/app/services/ingestion_pool.py : Singleton asyncpg PgPool (anila_core) opened at lifespan using DATABASE_URL/csp_app role so RLS enforces collection scope under SET LOCAL`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/migrations/versions/0014_add_ingestion_platform.py : Foundation migration — creates csp_app NOBYPASSRLS role, transfers table ownership, collections/documents/chunks/jobs schema, ENABLE+FORCE RLS (originally anila.agent_id GUC)`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/migrations/versions/0019_collection_first_class.py : Re-keys document_chunks RLS policy from anila.agent_id to anila.collection_id GUC; drops agent coupling, makes collections user-owned`
- `/home/aia/c1147259/ANILA/myCSPPlatform/backend/migrations/env.py : Alembic env — splits MIGRATION_DATABASE_URL (superuser, for CREATE EXTENSION/ROLE) from runtime DATABASE_URL (csp_app), imports all models for autogenerate`
- `/home/aia/c1147259/ANILA/myCSPPlatform/docker/docker-compose.yml : Topology — nginx (sole ingress, TLS) -> csp backend (:8000 internal) -> postgres:16 (pgvector); model/agent services join csp_network as commented examples`

**拓樸級風險**:
- Dual auth surface on the data plane: /v1/* accepts BOTH long-lived sk- API keys and short-lived JWTs through one get_caller dependency, while control-plane /api/* uses a separate get_current_user path — two credential lifecycles and revocation mechanisms (token_version bump vs key is_active) must be kept consistent or one path can outlive a deactivation.
- RLS isolation depends entirely on two runtime invariants holding together: (1) the app connecting as the non-privileged csp_app role (any connection as the superuser csp role or a BYPASSRLS role silently defeats FORCE ROW LEVEL SECURITY), and (2) every query path issuing SET LOCAL anila.collection_id before touching document_chunks. The GUC is also reachable only via the async anila_core pool, not the main SQLAlchemy session, creating two divergent DB access lanes.
- Cross-repo trust boundary via JWKS: CSP is the sole RS256 signer and downstream services (anila-studio etc.) verify against /.well-known/jwks.json with a single active kid (anila-v1) and no documented rotation path — a compromised/lost private PEM or a botched kid rotation breaks auth platform-wide.
- Privilege-split between migrations (escalated MIGRATION_DATABASE_URL superuser) and runtime (csp_app) means schema/role correctness is established only at migration time; the runtime role's ability to enforce RLS is a downstream consequence of migration 0014 ownership transfer succeeding — a partial/failed migration (the create_all fallback in lifespan is explicitly noted to create conflicting tables) can leave RLS in an inconsistent state.
- anila_core is an external in-tree dependency (storage adapters: PgPool, CollectionScopedPgVectorStore) shared with the ingestion worker; the search/embedding contract, vector dimension (halfvec 4000 truncation), and RLS scoping semantics are coupled across repo boundaries, so a contract change in anila_core can break CSP retrieval silently.
- nginx is the single declared ingress and the backend/postgres are only internally exposed; CORS allow_credentials and CSRF protection are conditional on ALLOWED_ORIGINS being explicitly configured (defaults fall back to '*' without credentials), so a misconfigured origin list at the topology edge changes the browser auth/cookie security posture.
- The data-plane proxy holds an in-process per-agent service-token cache and forwards X-CSP-Service-Token to downstream model/agent backends, plus a legacy CSP_SERVICE_TOKEN env fallback — service-to-service trust spans process memory + env + DB credentials, so the actual security boundary is wider than the request-level JWT/API-key gate.

### studio — `/home/aia/c1147259/ANILA/anila-studio/`

anila-studio is a standalone FastAPI "deck/artifact generation" service extracted from myCSPPlatform/backend. It owns no database and talks to the csp control plane HTTP-only (RAG chunk/image search, LLM proxy for billing, image blobs, collection metadata, JWKS, revocation sync), verifying RS256 JWTs locally against csp's JWKS plus a Redis-backed revocation deny-list. It runs five parallel async pipelines (slides/pptx, report, mindmap, infographic, datatable), each fronted by a POST-202 + poll + download API and an asyncio.Task-per-job in-memory state machine; pipelines orchestrate retrieve (csp_client.search_chunks/search_images) -> LLM generate (proxy_chat_completions) -> normalize -> render (external pptx-renderer / Graphviz / Playwright / pandoc) -> optional vision QA.

**技術棧**:Python 3.11, FastAPI + uvicorn (async), httpx (async HTTP client to csp/flux/renderer), python-jose[cryptography] (RS256 JWT + JWKS verify), pydantic v2 + pydantic-settings, redis (pub/sub revocation cache) + cachetools/fakeredis, asyncio Task-per-job in-memory orchestration, opencc (zh-CN -> zh-TW normalization), numpy (FFT striping detection), jinja2 + markdown-it-py (HTML templating), playwright (HTML->PDF), pypandoc (HTML->DOCX), matplotlib, openpyxl (XLSX), Graphviz CLI subprocess (diagram/mindmap render), external pptx-renderer Node service, FLUX backend, csp control plane, pytest + pytest-asyncio + respx (test stack), Docker (python:3.11-slim + apt graphviz, non-root)

**關鍵檔案**:
- `/home/aia/c1147259/ANILA/anila-studio/app/main.py : FastAPI entrypoint; lifespan boots JWKS + revocation cache (fail-closed), mounts the 5 artifact routers, exposes /health readiness gate (503 if Redis revocation cache not ready)`
- `/home/aia/c1147259/ANILA/anila-studio/app/config.py : pydantic-settings; all external endpoints (CSP_BASE_URL, FLUX_BACKEND_URL, RENDERER_BASE_URL, REDIS_URL), timeouts (30s default / 300s LLM), ARTIFACTS_DIR, FLUX_CACHE_DIR`
- `/home/aia/c1147259/ANILA/anila-studio/app/clients/csp_client.py : the SOLE egress to csp — async httpx wrappers (get_collection, search_chunks, search_images, fetch_image_blob, proxy_chat_completions); typed exception hierarchy mapping csp 401/403/404/5xx; exponential-backoff retry; per-call client (no pooling)`
- `/home/aia/c1147259/ANILA/anila-studio/app/auth.py : local RS256+JWKS JWT verify, Bearer-header-or-cookie sourcing, fail-closed revocation check; get_current_user_identity + get_bearer_token deps used by every handler for user-on-behalf-of csp calls`
- `/home/aia/c1147259/ANILA/anila-studio/app/api/studio.py : 3100-line slides pipeline + 4 endpoints; _run_pipeline orchestrates retrieve->LLM spec->normalize->layout audit/rebalance->render(pptx-renderer)->vision QA loop; hosts FLUX/Graphviz/screenshot integration`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/studio_job_service.py : reference in-memory job state machine — asyncio.Task per job, frozen immutable JobRecord, per-user isolation, FIFO eviction (8/user) + stale prune; pptx bytes held in memory`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/report_runner.py : two-stage (outline->draft) LLM report pipeline; canonical example of csp_client.search_chunks + proxy_chat_completions usage with per-section failure isolation`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/datatable_job_service.py : variant job service that persists artifacts to ARTIFACTS_DIR on disk (artifact_paths dict) instead of in-memory bytes — the divergent persistence model among job services`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/jwks_client.py : pulls csp /.well-known/jwks.json, caches public keys, lazy refresh on cache miss`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/revocation_cache.py : Redis pub/sub subscriber on anila:auth:token-revoke; 30-day TTL deny-list; .ready flag drives /health and fail-closed auth`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/flux_image_provider.py : FLUX backend HTTP client (image generation) used by slides hydration`
- `/home/aia/c1147259/ANILA/anila-studio/app/services/flux_quality_gate.py : VLM ranking + FFT striping detection for generated image candidates`
- `/home/aia/c1147259/ANILA/anila-studio/app/api/reports.py : reports POST-202/poll/download API; sibling routers mindmaps.py, infographics.py, datatables.py follow the same shape`
- `/home/aia/c1147259/ANILA/anila-studio/README.en.md : authoritative topology doc — lists every csp dependency endpoint, auth model, Redis channel`
- `/home/aia/c1147259/ANILA/anila-studio/pyproject.toml : dependency manifest revealing the render toolchain (jinja2, playwright, pypandoc, matplotlib, openpyxl, graphviz CLI)`

**拓樸級風險**:
- Hard dependency on csp control plane for ALL data + LLM + auth keys: anila-studio has no DB and cannot function if csp is down — JWKS, RAG search, LLM proxy, image blobs, and revocation cold-start all live there. csp is a single point of failure for the whole subsystem.
- In-memory job state (studio_job_service holds pptx bytes in a process-wide dict): all jobs and artifacts are lost on restart/crash; design assumes a single uvicorn worker — horizontal scaling or multiple workers would break job lookup (a job created on worker A is a 404 on worker B) and multiply memory pressure.
- Fail-closed coupling to Redis: if the Redis revocation channel disconnects, /health returns 503 and ALL authenticated endpoints return 503 (no degrade). Redis availability is therefore a hard dependency for serving any traffic, not just revocation.
- Multiple downstream service boundaries with no circuit breaker / shared client pool: csp_client opens a fresh httpx client per call (no connection pooling); pptx-renderer (RENDERER_BASE_URL), FLUX backend, and Graphviz CLI are each separate failure domains reached synchronously within the request/job path.
- Divergent job-persistence models across the 5 pipelines: slides keep artifact bytes in memory while report/mindmap/infographic/datatable write to ARTIFACTS_DIR on disk — two different lifecycle/cleanup contracts (memory eviction vs filename-overwrite on disk) increase operational surface and inconsistency risk.
- Configuration duplication at an integration boundary: RENDERER_BASE_URL is hardcoded in app/api/studio.py (line 145) AND defined as a settings field in config.py, so the slides path ignores the env-configurable value — an env override would silently not apply to slide rendering.
- Long-running LLM proxy path uses single-attempt, 300s timeout with no retry and relies on csp's internal LLM_TIMEOUT being raised to >=300 in production; a misconfigured csp timeout cascades into deck-pipeline failures (mitigated only by a fallback deck).
- Five sibling artifact subsystems (api/*.py + *_job_service.py + *_renderer.py) replicate the same POST-202/poll/download + asyncio-task pattern with near-duplicate code — topology-level maintenance risk: a contract or auth change must be applied consistently across all five.

### agent — `/home/aia/c1147259/ANILA/anila-agent/`

anila-agent is a clone-and-fill "Agentic RAG" starter package whose runtime is openai-agents SDK (Agent + Runner via LiteLLM, OpenAI-compatible endpoints) wrapped by a Claude-Code-style harness (hooks, file-based memdir memory, slash-command REPL). The wiring core is build_agent() in core/agent.py, which assembles the Agent, picks a retriever from env (anila_pgvector → pgvector → DummyRetriever), installs it via tools/rag_tools.set_retriever(), and loads built-in tools/hooks from configs/*.yaml. Sub-agents are turned into callable tools through the AgentTool spec + make_agent_tool factory (core/agent_tool.py), with a thin .as_tool() shorthand (core/agent_as_tool.py) that mirrors the upstream SDK API but adds prompt-cache prefix strategies, timeouts, and hook dispatch.

**技術棧**:Python >=3.10 (hatchling build, pyproject.toml), openai-agents[litellm] >=0.3.0 (Agent + Runner runtime, LiteLLM model provider), pydantic >=2.7 (schemas), pyyaml (config), python-dotenv (.env), prompt-toolkit + rich (REPL CLI / terminal rendering), OpenAI-compatible inference endpoints (vLLM / Ollama / OpenAI / Together), PostgreSQL + pgvector: asyncpg + halfvec/RLS (anila_pgvector), langchain-postgres + langchain-openai + psycopg (generic pgvector, optional extra), httpx (embedding HTTP calls in anila_pgvector), pytest + pytest-asyncio + pytest-cov, ruff, mypy (dev/test tooling), MCP server support (configs/tools.yaml mcp_servers, anila_agent/mcp)

**關鍵檔案**:
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/core/agent.py : build_agent() — central assembly; selects retriever via from_env() chain, calls set_retriever(), loads tools/hooks/memory, returns AssembledAgent`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/core/agent_tool.py : AgentTool dataclass + make_agent_tool() factory — wraps a sub-agent as a FunctionTool (sub-routine dispatch), prompt-cache prefix (share/fork), timeout, hook fire, error-to-JSON`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/core/agent_as_tool.py : as_tool() / as_tool_full() shorthand + enable_anila_as_tool_method() — ANILA equivalent of upstream Agent.as_tool(), thin delegation to make_agent_tool`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/core/coordinator.py : CoordinatorMessage + parse/format — XML notification protocol for parent<->sub-agent multi-agent messaging`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/retrieval/base.py : Retriever Protocol (search/fetch/name/metadata) — the single backend contract implementations satisfy`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/retrieval/anila_pgvector.py : AnilaPgVectorRetriever + from_env() — native ANILA schema (ingestion_collections/document_chunks, halfvec, RLS via anila.collection_id GUC); activated by ANILA_COLLECTION_ID`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/retrieval/pgvector.py : PgVectorRetriever + from_env() — generic langchain_postgres flavour, lazy-imported optional deps; activated by PGVECTOR_URL+PGVECTOR_COLLECTION`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/retrieval/dummy.py : DummyRetriever — in-memory token-overlap default retriever so the template runs with zero infra`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/tools/rag_tools.py : module-level search_documents/read_document tools + set_retriever()/get_retriever() — global retriever slot the tools route through`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/tools/registry.py : ToolRegistry + load_tools() — qualified-name tool loading, dedup, metadata filtering, deferred/ToolSearch activation`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/core/runner.py : AnilaRunner + RunSummary — thin wrapper over agents.Runner adding hook firing, session boundary events, abort handling`
- `/home/aia/c1147259/ANILA/anila-agent/anila_agent/main.py : CLI entry (anila) — load_config -> build_agent -> AnilaRunner; REPL or one-shot --prompt`
- `/home/aia/c1147259/ANILA/anila-agent/configs/tools.yaml : declares builtin tools, pre/post/stop hooks, mcp_servers — the wiring config build_agent reads`
- `/home/aia/c1147259/ANILA/anila-agent/README.md : architecture, layer-to-source map, retriever options A/B/C, dev/test workflow, CSP-template integration`
- `/home/aia/c1147259/ANILA/anila-agent/templete/README.md : upstream SDK source snapshots (claude-code-src / openai-agents-python / antigravity) as zip references, not runtime code`

**拓樸級風險**:
- Process-global mutable retriever: tools/rag_tools._retriever is a module-level singleton set via set_retriever(). build_agent() mutates it as a side effect, so concurrent/multi-tenant agents or multiple build_agent() calls in one process share/overwrite a single retriever — no per-agent or per-session isolation.
- Retriever selection is env-var driven at assembly time (ANILA_COLLECTION_ID > PGVECTOR_COLLECTION > Dummy). Topology binding is implicit/global rather than passed per-agent; a half-configured env (URL without collection) raises, and the active backend is invisible to callers except via logs.
- Sub-agent dispatch (AgentTool) runs each sub-agent through a separate Runner.run with its own (default) context and no shared memory/session — parent and sub-agent state are decoupled; prompt-cache prefix coupling depends on the caller injecting parent_messages into ctx.metadata, otherwise the share/fork cache benefit silently degrades to a baseline.
- Two coexisting as_tool surfaces (upstream SDK Agent.as_tool returning FunctionTool vs ANILA anila_as_tool/AgentTool with prefix+hook+timeout) create a dual-path topology; choosing the wrong one bypasses ANILA hook/timeout/cache behavior with no enforcement.
- This package is both a standalone template and the platform download template (mounted read-only into CSP via ANILA_TEMPLATE_DIR, also referenced by anila-core bootstrap and synced via git subtree). Changes to its structure or public surface ripple into the platform's /template/download artifact and the subtree contract.
- Optional pgvector deps are lazy-imported; the generic flavour silently disables unless the [pgvector] extra is installed, while anila_pgvector imports asyncpg/httpx inside methods — backend availability is a runtime-only failure mode, not a startup/assembly-time check.
- RLS scoping in anila_pgvector relies on `SET LOCAL anila.collection_id` injected via f-string (guarded only by an __init__ positive-int check) and on the DB role/policies being correctly configured; correctness of multi-collection isolation depends on out-of-repo Postgres RLS setup.

### lm — `/home/aia/c1147259/ANILA/ANILALM/`

test summary for anila lm subsystem with two backends and a pptx renderer

**技術棧**:TypeScript, React, Vite, react-router-dom, zustand, axios, fetch and EventSource, marked dompurify, openapi-typescript, Node Express pptxgenjs sharp jszip, LibreOffice and pdftoppm, Docker nginx

**關鍵檔案**:
- `src/App.tsx router and routes`
- `src/api/client.ts axios auth interceptor`
- `src/store/auth.ts auth store`
- `src/workspace/WSChat.tsx RAG chat`
- `src/workspace/WSStudio.tsx Studio panel`
- `src/workspace/CommandModal.tsx generation wizard`
- `src/api/studio.ts studio job client`
- `pptx-skill/server.js pptx renderer`
- `vite.config.ts dev proxy`
- `docker/nginx.conf static serve`
- `src/store/artifacts.ts artifact store`

**拓樸級風險**:
- Path-prefix routing split between dev proxy and prod proxy studio routes must precede the api catch-all
- Generated studio types from relative openapi path no CI gate so schema drift undetected
- Bearer tokens in localStorage but SSE needs a cookie EventSource cannot send Bearer
- Artifacts not server-persisted localStorage and in-memory jobs evicted on restart
- pptx-skill spawns LibreOffice subprocesses and takes server file paths a trust boundary
- Slides flow spans most hops assumes air-gapped on-prem
- Fixed BASE_PATH router basename nginx alias and proxy prefix must agree

### infra — `/home/aia/c1147259/ANILA/`

ANILA's infra subsystem is a Docker Compose multi-project topology where the sole external ingress is an nginx reverse proxy (TLS termination on :80/:443/:4443, self-signed cert) that fronts a CSP control/data-plane API, a router, two Vite SPAs (anilalm + anila-ui), anila-studio, plus cold-stored subpath services (code-server, n8n, GitLab). The platform stack (docker-compose.yml / docker-compose-dev.yml) is deliberately isolated from a separate inference stack (models/docker-compose.yml, GPU-pinned vLLM/TensorRT-LLM/Triton/FLUX) and the two communicate cross-project only over a shared external bridge network anila-models-net via Docker DNS; every backend service uses expose: (no host ports) except nginx and a loopback-bound Postgres, so the host's external NICs see only nginx. Secrets are env-driven (.env gitignored), startup_security gates dev-default secrets, and a service-to-service token plus an SSRF url_guard allowlist govern internal trust.

**技術棧**:Docker Compose (multi-project: anila-platform / anila-platform-dev / anila-models, Compose >=2.17 depends_on restart:true), Docker bridge networks (anila-net, anila-dev-net, external anila-models-net) + embedded DNS 127.0.0.11 for service discovery, nginx:alpine (reverse proxy, TLS termination, rate limiting, subpath routing, WebSocket upgrade), TLS 1.2/1.3 with self-signed cert (CN=localhost), HSTS + CSP + Permissions-Policy security headers, PostgreSQL via pgvector/pgvector:pg16 (loopback-bound, RLS-enforced csp_app role vs migration superuser), Redis 7-alpine (Arq queue + auth token-revocation pub/sub channel), Python 3.11 / FastAPI / uvicorn (CSP control+data plane, ingestion-worker, router, anila-studio), Vite/React SPAs (anilalm, anila-ui) served by in-container nginx, GPU inference: vLLM (gemma4), TensorRT-LLM (gpt-oss-20b), Triton (NV-Embed-v2), diffusers FLUX.2-dev — NVIDIA device reservations, Node + LibreOffice pptx-renderer; cold-stored code-server, n8n 1.98.2, GitLab CE 16.10, Secrets via env vars / .env (gitignored), service-to-service shared token, JWT HS256, SSRF url_guard host allowlist

**關鍵檔案**:
- `/home/aia/c1147259/ANILA/docker-compose.yml : production/live platform stack (project anila-platform) — defines csp, csp-db (pgvector, loopback :5433), redis, ingestion-worker, router, nginx ingress, anila-studio, anilalm, anila-ui, plus cold-stored codeserver/n8n/gitlab; wires service env, networks (anila-net + external anila-models-net), volumes, healthchecks, depends_on with restart:true`
- `/home/aia/c1147259/ANILA/docker-compose-dev.yml : fully isolated dev stack (project anila-platform-dev) — parallel ports (8080/8443/9443), distinct network anila-dev-net, container anila-nginx-dev, dev volumes and dev seed keys; adds image-generator agent auto-registration; shares only external anila-models-net so it doesn't collide with live`
- `/home/aia/c1147259/ANILA/myCSPPlatform/docker/nginx.conf : single external ingress / TLS termination — three server blocks (80 -> 301 https, 443 main app, 4443 ANILA UI), Docker-DNS resolver 127.0.0.11 with variable proxy_pass for re-resolution, rate-limit zones, security headers/HSTS/CSP, subpath routing for /api /v1 /v2 /router /codeserver /n8n /gitlab /anilalm /static /uploads`
- `/home/aia/c1147259/ANILA/models/docker-compose.yml : independent GPU inference stack (project anila-models) — gpt-oss-20b (TensorRT-LLM, GPU2), gemma4 (vLLM, GPU3), nv-embed-triton (GPU0, no expose) + nv-embed-proxy shim, flux2-dev (GPU1+2) + flux2-dev-agent; all expose:-only, joined to external anila-models-net; lifecycle decoupled from platform stack`
- `/home/aia/c1147259/ANILA/.env.example : root env template consumed by both platform compose files — CSP_SECRET_KEY, CSP_SERVICE_TOKEN, INTERNAL_PLATFORM_API_KEY, CODESERVER_PASSWORD/WORKSPACE, ANILA_ALLOW_DEV_SECRET + SSRF opt-in flags, model endpoint overrides; documents which vars are REQUIRED in prod`
- `/home/aia/c1147259/ANILA/myCSPPlatform/.env.example : legacy CSP backend env template (older AUTO_REGISTER_MODELS/LINKS pointing at vllm-llm/triton-embedding/mlsteam-host and ADMIN_PASSWORD=changeme) — superseded by root .env.example but still present`
- `/home/aia/c1147259/ANILA/myCSPPlatform/docker/Dockerfile : CSP image build — 2-stage (node:22 frontend build -> python:3.11-slim), installs anila-core[rag] then backend requirements, build context is repo root, EXPOSE 8000, uvicorn entrypoint`
- `/home/aia/c1147259/ANILA/myCSPPlatform/docker/certs/ : TLS material directory mounted read-only into nginx (server.crt/server.key, self-signed CN=localhost, valid 2026-2036); .gitignore here treats the cert+key pair atomically so neither is committed`
- `/home/aia/c1147259/ANILA/myCSPPlatform/docker/certs/.gitignore : documents the decision to gitignore BOTH crt and key (earlier asymmetric tracking caused nginx key-mismatch crashes on branch switch) and provides the openssl regen command with SAN`
- `/home/aia/c1147259/ANILA/.gitignore : repo-wide secret hygiene — ignores .env, .env.local, .env.*.local, *.pem, *.key (note: *.crt only ignored via the certs-local .gitignore, not the root one)`
- `/home/aia/c1147259/ANILA/myCSPPlatform/docker/docker-compose.yml : legacy/superseded single-project compose (containers csp-nginx etc., network csp_network) — predecessor of the root docker-compose.yml, retained but not the active topology`

**拓樸級風險**:
- Dev-default shared secrets ship as literal fallbacks across compose env (CSP_SECRET_KEY=dev-secret-key-change-in-prod, CSP_SERVICE_TOKEN=dev-service-token, INTERNAL_PLATFORM_API_KEY=sk-internal-worker-changeme, seed API keys); ANILA_ALLOW_DEV_SECRET defaults to 1 and docker-compose-dev.yml hardcodes it to "1" un-overridably — if a prod deploy forgets to unset these, the single service-to-service token and signing key are publicly known, collapsing all internal trust boundaries.
- Single self-signed TLS cert (CN=localhost, 10-year validity) is the only TLS identity for all three ingress ports and is regenerated per-host out of git, so there is no CA-backed identity; HSTS max-age 180d is asserted on a self-signed cert, which can lock browsers out if a real cert isn't installed before mass access.
- nginx is a single non-replicated ingress and a single point of failure for the entire platform (all external traffic for app, API, SPAs, GitLab, n8n, code-server funnels through one container); its depends_on uses restart:true to dodge pinned-upstream 502s, meaning an upstream rebuild cascades an nginx restart / brief outage.
- Trust is enforced largely by network isolation (expose:-only services, loopback-bound Postgres, no auth between CSP/router/ingestion/studio beyond a shared static token); anything that can join anila-models-net or anila-net (e.g. n8n Code nodes, code-server, a compromised container) can reach internal services and GPU model endpoints directly by DNS name, bypassing nginx rate limits and the API key layer.
- The external anila-models-net is shared by live, dev, and the GPU stack simultaneously; a dev stack misconfiguration (e.g. registering/poisoning a model or agent endpoint, or the dev seed keys) sits on the same L2 segment as production inference services, weakening the live/dev isolation the separate projects are meant to provide.
- Cold-stored high-privilege services (code-server, n8n, GitLab) are wired into nginx subpaths but their hardening is deferred (TODO: replace code-server PASSWORD and add nginx auth_request JWT check once SSO lands; n8n relies on a hardcoded module allowlist to avoid arbitrary-require RCE); enabling them before SSO/auth_request is in place exposes RCE-capable surfaces behind only a static password.
- *.crt is NOT covered by the root .gitignore (only by the certs-dir local .gitignore), and a server.crt.bak plus an old tracked-cert history exist — a future move of cert material or a new cert dir outside myCSPPlatform/docker/certs/ could silently start committing certificate material.
- SSRF posture depends on env flags: ANILA_ALLOW_HTTP_ENDPOINT / ANILA_ALLOW_PRIVATE_ENDPOINT default to opt-in and the root .env enables both; combined with the ANILA_TRUSTED_HOSTS allowlist that must be hand-edited when new model services are added, a stale or over-broad allowlist can let CSP proxy out to unintended LAN/private endpoints.


## 附錄 B:已驗證 findings(43 條,走完對抗驗證)


### 資安(10)

#### 🟠 HIGH · Production .env disables the startup secret guard (ANILA_ALLOW_DEV_SECRET=1) while also leaving ADMIN_PASSWORD at default 'changeme' -> bootable owner account with known password

- **可信度**:✅ 已驗證 — 投票 3/3(finder confidence=high)
- **類別**:auth/secrets
- **證據**:/home/aia/c1147259/ANILA/.env:18 sets ANILA_ALLOW_DEV_SECRET=1 even though the same file's header (lines 7,17) declares this is 'Production initial mode' and that prod MUST unset/0 this flag. Neither /home/aia/c1147259/ANILA/.env nor docker-compose.yml sets ADMIN_PASSWORD, so app/config.py:40 default ADMIN_PASSWORD='changeme' applies. app/services/auto_seed.py:97-107 creates the first user as role='owner', is_active=True, hashed_password=hash_password(settings.ADMIN_PASSWORD) on a fresh DB. app/services/startup_security.py:41 lists 'changeme' as a known ADMIN_PASSWORD default, and assert_no_dev_defaults() (app/main.py:94-95) only downgrades that to a warning instead of RuntimeError when ANILA_ALLOW_DEV_SECRET=1 (startup_security.py:52-53,114-120). Net effect: a prod deploy using this .env boots an owner-tier account admin/changeme that can immediately log in (no is_approved gate on the seeded owner).
- **建議**:In the prod .env remove ANILA_ALLOW_DEV_SECRET (or set 0) so startup_security.assert_no_dev_defaults() hard-fails on any remaining default, and explicitly set a strong ADMIN_PASSWORD (and ADMIN_USERNAME) before first boot. Consider forcing a password change / disabling the seeded owner after bootstrap, and have the guard treat ADMIN_PASSWORD='changeme' as a fatal offender (not just a warning) even in dev when COOKIE_SECURE/prod indicators are present.

#### 🟠 HIGH · Audit log rows for all ingestion-platform sensitive operations are never persisted (silent audit-trail loss)

- **可信度**:✅ 已驗證 — 投票 3/3(finder confidence=high)
- **類別**:audit-logging
- **證據**:app/services/audit_service.py:18,31-34 — log_audit_event adds the AuditLog to the session but only flushes/commits when commit=True. app/database.py:13,20-25 — SessionLocal is autocommit=False and get_db only closes (no commit) on teardown, so a session closed with pending writes rolls them back. The ingestion endpoints call log_audit_event AFTER their final db.commit() and without commit=True: app/api/ingestion/collections.py:122->131 (create), :226->235 (update), :266->267 (delete); app/api/ingestion/documents.py:245->247 (upload), :466->474 (zip upload), :612->636 (delete); app/api/ingestion/eval_runs.py:231->234 (create); app/api/ingestion/credentials.py:120->129 (create), :184->186 (update), :210->211 (delete). In each, the audit row is added post-commit and the function returns immediately, so the row is discarded on session close. Contrast the correct pattern in app/api/auth.py:130-137, app/api/users.py:104-111, app/api/service_clients.py:175-186 (db.flush()->log->db.commit()), and app/api/alerts.py:77-85 — all of which pass commit=True or commit afterward. No test asserts ingestion audit persistence (tests/ has no ingestion+audit assertions).
- **建議**:Pass commit=True to every log_audit_event call in the ingestion endpoints (collections.py, documents.py, eval_runs.py, credentials.py), or add a single db.commit() immediately after each call. Add a regression test that hits each ingestion mutation endpoint and asserts a corresponding audit_log row exists. For NCSIST intranet compliance, treat 'mutation succeeds but no audit row' as a release-blocking defect.

#### 🟡 MEDIUM · No nginx Host-header allowlist and no FastAPI TrustedHostMiddleware (the assumed is_anila_host map does not exist)

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:Host header injection
- **證據**:The audit premise references an `is_anila_host` map in myCSPPlatform/docker/nginx.conf, but no such map exists anywhere in the repo (grep for `is_anila_host` returns nothing in any .conf/.py/.yml). Every nginx server block uses a catch-all `server_name _;` (myCSPPlatform/docker/nginx.conf:53, :414) and accepts any Host header. The backend proxies the client-controlled Host downstream: `proxy_set_header Host $host;` on the /api/, /v1/, / locations (nginx.conf:119,133,232,387, etc.). CSP FastAPI (myCSPPlatform/backend/app/main.py:216-230) adds only CORSMiddleware and CsrfMiddleware — there is no Starlette TrustedHostMiddleware / allowed_hosts gate. An attacker can therefore send an arbitrary Host (e.g. via a forged absolute request or a malicious intermediary), which is reflected in `$host`-derived redirects (nginx.conf:54 `return 301 https://$host$request_uri`) and forwarded to the backend — enabling Host-based cache poisoning and password-reset / absolute-URL poisoning if any backend code builds links from the Host header.
- **建議**:Add an explicit Host allowlist: either an nginx `map $host $is_anila_host { default 0; <prod-domains> 1; }` plus `if ($is_anila_host = 0) { return 444; }` in each server block, or a Starlette `TrustedHostMiddleware(allowed_hosts=[...])` in myCSPPlatform/backend/app/main.py driven by an env allowlist. Replace `$host` with a fixed canonical hostname in the HTTP->HTTPS redirect to stop redirect/cache poisoning.

#### 🟡 MEDIUM · Default ANILA_TRUSTED_HOSTS in docker-compose includes host.docker.internal, fully bypassing the SSRF deny-list

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:SSRF
- **證據**:docker-compose.yml:56 ships `ANILA_TRUSTED_HOSTS: "${ANILA_TRUSTED_HOSTS:-gpt-oss-20b,gemma4,nv-embed-proxy,host.docker.internal}"` as the default. In anila-core/src/anila_core/security/url_guard.py:281-282, a host present in the trusted set returns immediately, skipping every subsequent check — the loopback/metadata `_DENY_HOSTS` (which includes 169.254.169.254, url_guard.py:120-128), the internal-zone suffixes, the private-IP rules, and DNS resolution. `host.docker.internal` resolves to the Docker host gateway, so any logged-in user can register an agent/credential endpoint at `https://host.docker.internal:<port>/...` and have the worker/proxy POST to arbitrary services bound on the host — and, on cloud hosts where the host can reach 169.254.169.254, potentially pivot to instance metadata. The single-label service names (gemma4 etc.) are intended, but host.docker.internal is a broad host-gateway escape hatch enabled by default.
- **建議**:Remove `host.docker.internal` from the default ANILA_TRUSTED_HOSTS in docker-compose.yml; require operators to opt it in explicitly only for dev. Consider keeping the metadata/loopback deny-list enforced even for trusted hosts (i.e. apply `_is_always_unsafe_ip` / `_DENY_HOSTS` before the trusted-host early-return in url_guard.py).

#### 🟡 MEDIUM · ingestion_images vector table has no RLS policy — cross-collection isolation relies solely on application-layer WHERE filters

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:rls
- **證據**:migrations/versions/0026_add_ingestion_images.py:82-124 creates ingestion_images with a denormalised collection_id column and a plain btree index (ix_images_collection) but never runs ENABLE/FORCE ROW LEVEL SECURITY and creates no policy. grep shows 0026 is the only migration referencing ingestion_images and no images RLS policy exists anywhere. By contrast document_chunks gets ENABLE+FORCE RLS in 0014:365-366 plus the chunks_collection_isolation policy (0019:90-101), backed by the NOBYPASSRLS NOSUPERUSER csp_app role (0014:108-109) and verified by anila-core/tests/integration/test_g2_rls_bypass.py. The two readers of ingestion_images do check access at the app layer — app/api/ingestion/search.py:363 (_require_collection_access) + :421 (WHERE i.collection_id = $1), and app/api/ingestion/image_blob.py:104-109 (resolve collection_id from row, then _require_collection_access) — so there is no IDOR today. The gap is purely defense-in-depth: search.py:406 acquires a raw pool.acquire() connection (not CollectionScopedPgVectorStore._acquire), so no SET LOCAL anila.collection_id is issued, and any future query/endpoint that forgets the WHERE collection_id filter would leak images cross-collection with no engine backstop.
- **建議**:Add ENABLE + FORCE ROW LEVEL SECURITY on ingestion_images plus an images_collection_isolation policy keyed on current_setting('anila.collection_id'), mirroring document_chunks. Then route the image search query (search.py:406) through a CollectionScopedPgVectorStore-style _acquire that sets SET LOCAL anila.collection_id so the engine enforces scope. Extend the G2 RLS-bypass integration test to cover ingestion_images.

#### ⚪ LOW · SSRF guard validates endpoint_url only at create/update, not at call time (stored-SSRF / DNS-rebinding TOCTOU)

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:SSRF
- **證據**:validate_outbound_url is enforced at registration/update for agents (myCSPPlatform/backend/app/api/agents.py:243,349), models (myCSPPlatform/backend/app/api/models.py:148,297-298), and ingestion credentials (myCSPPlatform/backend/app/api/ingestion/credentials.py:107,177). But the outbound call paths read the stored endpoint without re-validating: proxy_service.py:411/433/580/622 (`model.endpoint_url`), proxy.py:476/521/668 (`agent.endpoint_url`), agents.py:559 and models.py:465 (admin health-check probes). Only the ingestion worker re-validates defense-in-depth (ingestion-worker/src/ingestion_worker/judge.py:169-176). This leaves a TOCTOU/DNS-rebinding window where a hostname that resolved to a public IP at create-time can later resolve to 169.254.169.254 / a private host at call-time. The url_guard.py module docstring (lines 37-41) explicitly acknowledges this DNS-rebinding gap. Additionally, agents.py:534 health-check probes any agent including approval_status='pending' ones.
- **建議**:Add a call-time validate_outbound_url() (or resolve-then-pin-IP) in proxy_service.py and proxy.py before issuing the httpx request, mirroring the worker's defense-in-depth pattern. At minimum re-validate in the admin health-check endpoints. Consider resolving the host once and connecting to the validated IP to close the rebinding window.

#### ⚪ LOW · CORS falls back to wildcard allow_origins=["*"] when ALLOWED_ORIGINS is empty

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:CORS misconfiguration
- **證據**:myCSPPlatform/backend/app/main.py:222 sets `allow_origins=_allowed_origins or ["*"]`. If ALLOWED_ORIGINS is set to an empty string in an environment (the config default at myCSPPlatform/backend/app/config.py:69 is a localhost list, but prod is instructed to override it), `_allowed_origins` becomes `[]` and the code falls back to `["*"]`. With `allow_credentials=bool(_allowed_origins)` correctly becoming False in that case, cookie-based CSRF is not directly exposed, but Bearer/API-key flows from any origin become permitted and `allow_methods=["*"]`/`allow_headers=["*"]` widen the surface. A misconfigured (empty) ALLOWED_ORIGINS silently degrades to fully open CORS rather than failing closed.
- **建議**:Fail closed: when ALLOWED_ORIGINS resolves to empty in a non-dev environment, refuse to start or default to a deny-all origins list rather than `["*"]`. Gate the wildcard behind an explicit dev flag (e.g. ANILA_ALLOW_DEV_SECRET) so production cannot accidentally open CORS.

#### ⚪ LOW · DB driver error details (e.orig) leaked to API clients on ingestion collection create/update

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:data-exposure
- **證據**:app/api/ingestion/collections.py:127 — detail=f"Collection creation failed: {e.orig}" and :231 — detail=f"Update failed: {e.orig}" embed the raw psycopg/SQLAlchemy IntegrityError .orig (which can include constraint names, column names, and DB-internal phrasing) directly into the 500 response body returned to the caller. Similar raw-exception interpolation appears at app/api/agents.py:594 and app/api/models.py:509 (detail includes ({e})). FastAPI itself is safe by default (no global exception handler echoing tracebacks; settings.DEBUG defaults False per app/config.py:9), so this is limited to these explicit interpolations rather than systemic stack-trace leakage.
- **建議**:Return a generic client-facing message (e.g. 'Collection name conflict or invalid input') and log the full e.orig server-side via logging instead of placing it in the HTTP detail. Reserve schema-level error text for owner/admin-only diagnostic endpoints. Relevant for NCSIST intranet hardening where schema/constraint disclosure aids an internal attacker.

#### ℹ️ INFO · Swagger UI and OpenAPI schema served without authentication

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:data-exposure
- **證據**:app/main.py:244-251 mounts GET /docs (custom_swagger_ui) with no auth dependency, and FastAPI serves /openapi.json by default; both expose the full API surface (every ingestion/admin route, parameters, schemas) to any unauthenticated caller that can reach the service. docs_url/redoc_url are disabled (:182-183) only to swap in the offline Swagger bundle, not to gate access.
- **建議**:For the NCSIST intranet deliverable, gate /docs and /openapi.json behind authentication (e.g. require_admin) or disable them in production via a config flag. Low risk on a closed intranet but it broadens reconnaissance for an internal actor.

#### ℹ️ INFO · document_chunks RLS GUC is correctly set on every read/write path through the store and agent retriever

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:rls
- **證據**:Positive finding. CollectionScopedPgVectorStore._acquire (anila-core/src/anila_core/storage/adapters/pgvector_store.py:79-102) wraps every connection in a transaction and issues SET LOCAL anila.collection_id before yielding; all read/write methods (index_chunks, similarity_search, keyword_search, list_by_document, list_in_collection, delete_document, delete_all, add_parent_chunks) and _attach_parent_content (which additionally filters AND collection_id = $2, :376-379) run inside _acquire. The constructor rejects non-positive / non-int / bool collection_id (:61-71). The agent-side AnilaPgVectorRetriever.search (anila-agent/anila_agent/retrieval/anila_pgvector.py:152-156) also opens a transaction and sets SET LOCAL anila.collection_id before querying document_chunks. The only document_chunks raw-SQL access outside _acquire is the embedding-debug endpoint (documents.py:788), which deliberately uses store._acquire() so the GUC is still set. Engine-level enforcement is verified by ENABLE+FORCE RLS (0014:365-366) on a NOBYPASSRLS role and the G2 integration test.
- **建議**:No action required for document_chunks. Keep the G2 RLS-bypass test in CI and extend it to ingestion_images (see separate finding).


### UIUX(19)

#### 🟠 HIGH · Uploaded document can get stuck on a non-terminal status forever with no polling fallback or manual refresh

- **可信度**:✅ 已驗證 — 投票 3/3(finder confidence=high)
- **類別**:Invisible state / dead-end (Journey 2: upload → ingest → wait for chunks)
- **證據**:WorkspacePage.tsx:47-72 fetches listDocuments() exactly once on mount and never again. Live progress depends entirely on useJobStream.ts:20-21, which only opens an SSE stream for a doc whose `jobId !== undefined`. The jobId is only populated in WSSidebar.tsx:64-69 via a best-effort getDocument() follow-up: if that call fails (catch at WSSidebar.tsx:68 swallows it: 'best-effort; user still sees the doc, just no live progress'), OR if the worker hasn't created the ingestion_job row yet at the moment of the follow-up (race — upload returns 202 with a pending row before the job is registered), the doc stays with no jobId. With no jobId there is no SSE subscription, no polling, and no re-fetch of the document list. The sidebar row (WSSidebar.tsx:280-378) will display '排隊中'/'解析中' (or whatever the initial status was) indefinitely until the user manually leaves and re-opens the collection. There is no refresh button on the sidebar document list (the comment at WSSidebar.tsx:126-129 explicitly drops the refresh helper).
- **建議**:Add a fallback: when a doc lacks a jobId but its status is non-terminal (pending/parsing/chunking/embedding/queued), poll getDocument() on an interval (e.g. 3-5s) until latest_job_id appears (then bind SSE) or status becomes terminal. Alternatively expose a manual 'refresh sources' affordance in the sidebar header so the user can recover from a missed job binding without navigating away.

#### 🟡 MEDIUM · Studio 'pending' job cannot be cancelled from the UI — cancel API exists but no component calls it

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:Missing affordance / dead-end (Journey 4: studio job submission + monitoring)
- **證據**:cancelSlidesJob and cancelReportJob/cancelMindmapJob/cancelInfographicJob/cancelDatatableJob are all implemented (api/studio.ts:223, 420-421, 460-461, 498-499, 538-539) but a repo-wide grep shows they have ZERO callers in src/*.tsx. The WSStudio timeline (WSStudio.tsx:559-788) renders a pending artifact with a spinner ('鑄造中', line 683) and only offers a 刪除 (remove-from-list) button (WSStudio.tsx:761-783) which calls removeArtifact — that just drops the local artifact row and stops the poller; it does NOT DELETE the server job. A user who launched a wrong 60-180s job has no way to actually cancel the backend work; they can only hide it locally and wait for it to finish/fail on the server.
- **建議**:Wire a cancel button on pending timeline rows that calls the kind-appropriate cancel*Job(jobId), then patches the artifact to 'failed'/'cancelled'. At minimum, make the 刪除 action on a pending artifact also fire cancel*Job so removing it from the timeline tears down the server job rather than orphaning it.

#### 🟡 MEDIUM · Inline [N] citation markers in chat answers are plain text and not clickable — no click-through to the source

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:Missing affordance / page-switching friction (Journey 3: chat citation click-through)
- **證據**:The system prompt instructs the model to cite as `[N]` inline (WSChat.tsx:217-218 '引用時用 [N] 標號'). The assistant text is rendered through MarkdownPreview (WSChat.tsx:676) which just runs marked→DOMPurify and dumps HTML via dangerouslySetInnerHTML (MarkdownPreview.tsx:37) — the `[1]`/`[2]` tokens stay as inert text. The separate CitationStrip below the bubble (WSChat.tsx:686-797) renders clickable cards keyed by chunk_id, but there is no link between an inline `[1]` in the prose and card #1; clicking the prose marker does nothing. The dedicated clickable Cite component (components/Cite.tsx, supports onClick) is never imported by WSChat. Additionally, expanding a citation card only reveals a 240-char excerpt (WSChat.tsx:305 excerpt = h.content.slice(0,240); rendered at WSChat.tsx:770-788) — there is no way to open the full chunk or the source document from chat (documentBlobUrl exists in documents.ts:45 but is unused).
- **建議**:Post-process the rendered markdown to turn `[N]` into clickable anchors that scroll to / highlight the matching CitationStrip card (reusing the Cite component). Add a 'view full chunk / open document' action on the expanded citation card using listDocumentChunks or documentBlobUrl so users can verify provenance without leaving the chat.

#### 🟡 MEDIUM · Small-text muted colors (textSubtle) fail WCAG AA contrast in both light and dark themes

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:accessibility
- **證據**:src/theme/tokens.ts:44,56 define textSubtle as #6B7280 (dark) / #8B919C (light). This token is applied to ~25 small-text elements (10.5px–11px), e.g. WSSidebar.tsx:524 conversation timestamps, WSChat.tsx:450 'sources' badge / WSChat.tsx:760 chunk metadata, DashboardPage.tsx:548 card timeAgo. Measured contrast: dark textSubtle on surface2 #191D24 = 3.50:1, light textSubtle on surface #FFFFFF = 3.17:1, light textSubtle on bg #FAFAF7 = 3.03:1 — all below the 4.5:1 AA threshold for normal text (and well below since text is <14px). The Login terminal 'dim' color in light mode (#8B919C on #F5F4EE) is 2.88:1.
- **建議**:Darken light-theme textSubtle toward ~#6B7280 (and dark-theme textSubtle toward ~#828a96) so secondary metadata at 10.5–11px reaches >=4.5:1, or raise those font sizes. Verify with an automated contrast check across token pairs.

#### 🟡 MEDIUM · Modal lacks dialog semantics and focus management (no role/aria-modal, no focus trap, no focus restore)

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:accessibility
- **證據**:src/components/Modal.tsx renders a plain <div> overlay + <div> panel with no role="dialog", aria-modal, or aria-labelledby (grep for role= returned nothing across src/). It handles Escape (Modal.tsx:17-22) but does not trap Tab focus inside the dialog, does not move focus into the dialog on open, and does not restore focus to the trigger on close (no focus/tabIndex code in Modal.tsx or CommandModal.tsx). Screen-reader users are not told a dialog opened, and keyboard focus can drift behind the backdrop. This affects all modals (CreateCollectionModal, CommandModal, ArtifactViewer).
- **建議**:Add role="dialog" aria-modal="true" and aria-labelledby pointing at the title; on open, focus the first interactive element (or the dialog) and trap Tab within it; on close, restore focus to the invoking control.

#### 🟡 MEDIUM · ErrorBoundary 'reset' button hardcodes the /anilalm/ base path, breaking recovery in other deployments

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:error-state
- **證據**:src/components/ErrorBoundary.tsx:88 does window.location.replace('/anilalm/') after clearing localStorage. The app's base path is configurable: App.tsx:34 derives ROUTER_BASENAME from import.meta.env.BASE_URL and vite.config.ts:16-19 sets base from BASE_PATH/VITE_BASE_PATH defaulting to '/'. In local dev (base '/') or any non-'/anilalm/' mount, the crash-recovery button redirects to a path that 404s or leaves the SPA, so the user cannot recover from a render crash.
- **建議**:Replace the literal with import.meta.env.BASE_URL (the same source App.tsx uses), e.g. window.location.replace(import.meta.env.BASE_URL || '/').

#### 🟡 MEDIUM · Destructive and error flows rely on native confirm()/alert(), inconsistent with the in-app modal/toast design

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:consistency
- **證據**:Destructive actions and upload/delete errors use browser-native dialogs: DashboardPage.tsx:98 confirm() + :103 alert() for collection delete; WSSidebar.tsx:72/92/109/122 alert() for upload/conversation/document errors and :97/:114 confirm() for deletes. The rest of the app has a polished themed Modal (components/Modal.tsx) and inline error banners (e.g. DashboardPage.tsx:294-330, WSChat.tsx:512-525). Native dialogs are unstyled, ignore dark/light theme, cannot be themed for zh-TW typography consistency, and break the otherwise cohesive visual language.
- **建議**:Route destructive confirmations through the existing Modal and surface action errors via the existing inline error-banner pattern (or a small toast) instead of alert()/confirm(), so error/confirmation states match the rest of the UI.

#### ⚪ LOW · Upload progress callback is a no-op — large-file upload shows only a generic 'uploading...' with no per-file or percentage feedback

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:Invisible state (Journey 2: upload)
- **證據**:uploadDocument supports an onProgress fraction callback (documents.ts:24-43, wired to axios onUploadProgress). But WSSidebar.tsx:60 calls uploadDocument(collection.id, file, () => undefined) — it passes a no-op, then WSSidebar.tsx:62 calls setUploadFraction(doc.id, undefined). The store carries an uploadFraction field (store/workspace.ts:11, :61-66) but it is never set to a real value and never read anywhere in the render. During upload the sidebar shows one shared spinner '上傳中...' (WSSidebar.tsx:249-252) for the whole batch with no per-file name or percentage. For a multi-file or large-PDF upload over a slow link the user gets no sense of which file is uploading or how far along it is.
- **建議**:Pass a real onProgress callback that calls setUploadFraction(doc.id, fraction) and render the fraction on the in-flight upload row, or at least show the current filename and N/total during a batch upload.

#### ⚪ LOW · Chat send is gated behind ⌘/Ctrl+Enter only — plain Enter does nothing and there is no on-screen hint beyond placeholder text

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:Affordance friction (Journey 3: chat)
- **證據**:onComposerKey (WSChat.tsx:399-404) only triggers send() when e.key === 'Enter' && (e.metaKey || e.ctrlKey). A plain Enter inserts a newline and never sends. The only indication is the placeholder text '問點什麼... (⌘ + Enter 送出)' (WSChat.tsx:547), which disappears as soon as the user starts typing. Users coming from typical chat UIs (Enter to send) will press Enter, get a newline, and may not realize how to submit; the send button (WSChat.tsx:591-611) is the only always-visible path.
- **建議**:Either support plain Enter to send with Shift+Enter for newline (the more common chat convention), or keep a persistent hint near the send button (not just in the placeholder) so the shortcut stays discoverable after typing begins.

#### ⚪ LOW · Job-stream SSE silently fails on auth/connection drop with no user-visible reconnection or error state

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:Invisible state (Journey 2: ingest progress)
- **證據**:streamJob (api/jobs.ts:12-43) opens an EventSource that depends on a cookie session (jobs.ts:3-6) rather than the Bearer token used everywhere else. useJobStream.ts:37 passes a no-op onError (`() => undefined`). On es.onerror (jobs.ts:36-38) the only action is to call onError when CLOSED — which is a no-op here — so a dropped/expired SSE connection produces no UI signal and no reconnect attempt. The doc row will simply freeze at its last received status. Because SSE relies on the login cookie while the rest of the app refreshes Bearer tokens, an expired cookie (without a corresponding refresh path for EventSource) can silently stop progress updates while the rest of the app keeps working.
- **建議**:Surface SSE failure on the affected doc row (e.g. a 'connection lost — retry' state) and implement a bounded reconnect, or fall back to getDocument() polling when the stream errors. Verify the SSE cookie session lifetime matches the access/refresh token lifetime so progress streams don't outlive their auth.

#### ⚪ LOW · White button text on accent fails AA in dark theme

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:accessibility
- **證據**:Primary buttons use color:'#fff' on background t.accent throughout (e.g. DashboardPage.tsx:279 '新建知識庫', CommandModal.tsx:654/678 '繼續'/'開始鑄造', WSChat.tsx send button). In dark theme accent is #7C7BFF (tokens.ts:38); white-on-#7C7BFF measures 3.42:1, below the 4.5:1 AA threshold for the 12.5–14px button labels. Light theme accent #5957E8 passes at 5.31:1.
- **建議**:Use a slightly darker accent for dark-theme primary buttons (e.g. ~#6361E0) or keep #7C7BFF only for >=18px/bold text where 3:1 large-text AA applies. Button labels here are normal-size.

#### ⚪ LOW · Dead/unused UI components (EmptyState, Cite) diverge from the components actually rendered

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:consistency
- **證據**:src/components/EmptyState.tsx is never imported (grep shows only WSChat's separate local ChatEmptyState at WSChat.tsx:505/799 is used). src/components/Cite.tsx is never imported anywhere (WSChat renders its own CitationStrip at WSChat.tsx:686 instead). These shared components define empty-state and citation visual patterns that the live screens reimplement independently, so the 'canonical' component and the rendered one can (and do) drift.
- **建議**:Either adopt EmptyState/Cite in the live workspace screens for a single source of truth, or delete them to avoid maintainers editing the wrong (dead) component.

#### ⚪ LOW · Stale 'MVP only supports 報告/簡報' empty-state copy contradicts current capability (all 5 formats supported)

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:consistency
- **證據**:CommandModal.tsx:140-141 sets isSupported true for report, slides, mindmap, infographic, AND datatable. But the !isSupported fallback branch (CommandModal.tsx:337-356) still renders copy claiming 'MVP 只支援「深度報告」與「簡報」兩種輸出。其餘類型會在後端...就緒後解鎖。' Since every FORMATS entry is now supported, this branch is unreachable but the wording is factually wrong if a future format is added back, and it misrepresents the product. Relatedly, FormatSpec.comingSoon (CommandModal.tsx:33, rendered as a 'SOON' badge in WSStudio.tsx:486-500) is never set on any FORMATS entry, so the badge is dead.
- **建議**:Update the unsupported-format copy to reflect current capabilities (or remove the dead branch), and remove the unused comingSoon/'SOON' badge path unless a roadmap format is reintroduced.

#### ⚪ LOW · WSStudio category filter type includes 'audio'/'study' options that no longer exist

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:consistency
- **證據**:WSStudio.tsx:140 types filter state as 'all' | 'audio' | 'visual' | 'study' | 'doc', but CATEGORIES (WSStudio.tsx:104-108) only renders 'all'/'visual'/'doc' (audio + study were intentionally removed per the comment at :102-103). The dead union members can never be selected via the UI, and FORMATS only carries cat 'visual'|'doc' (CommandModal.tsx:31), so the extra filter values are unreachable leftovers.
- **建議**:Narrow the filter state type to 'all' | 'visual' | 'doc' to match the rendered CATEGORIES and FormatSpec.cat, eliminating the stale options.

#### ⚪ LOW · Markdown-rendered links have no target/rel handling; assistant output links navigate in-place with no noopener

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:consistency
- **證據**:components/MarkdownPreview.tsx:19-26 runs marked → DOMPurify but adds no link hardening: no ADD_ATTR for target/rel and no transform hook (grep for target/rel/ADD_ATTR returned nothing). LLM/report markdown links therefore render as default <a> with no target=_blank and no rel="noopener noreferrer". Clicking a citation/source link replaces the SPA (losing the conversation/job state) and, if opened in a new tab manually, lacks noopener.
- **建議**:In the DOMPurify config add a hook to set target="_blank" rel="noopener noreferrer nofollow" on anchors (and allow the target/rel attributes), so external links open safely without discarding workspace state.

#### ⚪ LOW · Login form sets minimal autoComplete and bypasses native validation, weakening keyboard/AT form UX

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:accessibility
- **證據**:LoginPage.tsx:222 uses a custom <form> whose TerminalField inputs have no id/name beyond the DOM id used for click-to-focus (LoginPage.tsx:390) and no associated <label htmlFor> — labels are plain <span> text (LoginPage.tsx:389), so the input has no programmatic label/name for screen readers or password managers. Validation is JS-only (LoginPage.tsx:66-73) with no required/aria-invalid, and the error region (LoginPage.tsx:255-269) is not announced (no role="alert"/aria-live).
- **建議**:Associate each input with a real label (htmlFor/id or aria-label), add name attributes, and mark the error banner with role="alert" (aria-live="polite") so validation failures are announced to assistive tech.

#### ⚪ LOW · Inline error banners are not announced to assistive technology (no aria-live/role=alert)

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:accessibility
- **證據**:Error states render as styled divs with no live-region semantics: DashboardPage.tsx:294 collection-load error, WSChat.tsx:512 chat error, CommandModal.tsx:580 generation error, WorkspacePage.tsx:105 workspace-load error, StudioWizard/ArtifactViewer failure text (ArtifactViewer.tsx:277-283). None use role="alert" or aria-live, so screen-reader users who trigger an async failure (e.g. a failed send or upload) receive no notification that an error appeared.
- **建議**:Add role="alert" or aria-live="assertive/polite" to the error banner containers so dynamically inserted error messages are announced.

#### ⚪ LOW · No prefers-reduced-motion handling for the app's animations

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:accessibility
- **證據**:index.html:42-56 defines pulse/fadeIn/slideUp/spin keyframes used pervasively (Modal.tsx:37,56 fadeIn/slideUp on every dialog; Spinner.tsx:20 spin; WSSidebar.tsx:365 pulsing processing dot). There is no @media (prefers-reduced-motion: reduce) rule anywhere (grep returned nothing), so users who request reduced motion still get the modal slide/scale and continuous pulse animations.
- **建議**:Add a prefers-reduced-motion media block in index.html that disables or shortens the fadeIn/slideUp/pulse/spin animations.

#### ℹ️ INFO · Auth store records raw axios message as login error while UI uses explainError — inconsistent error text source

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:error-state
- **證據**:store/auth.ts:42 stores error: err.message on login failure, which for an axios 401 is the generic 'Request failed with status code 401'. LoginPage.tsx:87 instead displays explainError(anyErr) (client.ts:92 extracts the backend {detail} message), so the user sees a good message. The store's error field is just never surfaced — but if any future screen reads useAuthStore().error it will show the unfriendly raw message, diverging from the rest of the app's error presentation.
- **建議**:Store explainError(err) in the auth store's error field for consistency, so any consumer of state.error gets the same backend-derived message the login screen shows.


### 可改進(14)

#### 🟠 HIGH · LLM-JSON parse/repair helpers (_extract_json_object / _loads_lenient) duplicated and already divergent across 3 studio API modules

- **可信度**:✅ 已驗證 — 投票 3/3(finder confidence=certain)
- **類別**:duplicated logic
- **證據**:Identical-purpose lenient-JSON helpers are independently defined in anila-studio/app/api/studio.py:179,239, anila-studio/app/api/datatables.py:234,268, and anila-studio/app/api/infographics.py:121,167. anila-studio/app/api/mindmaps.py:91-96 instead imports them from studio.py with a comment claiming dedup. The copies have already drifted: diffing studio.py's _extract_json_object against datatables.py's yields ~78 differing lines. These helpers absorb model-specific malformed-JSON quirks (think-block stripping, brace-walking, single-quote repair); a fix applied to one copy (e.g. a new gemma4 output shape) will not reach the other two, producing inconsistent spec-parsing behavior per artifact family.
- **建議**:Extract a single anila-studio/app/services/llm_json.py (or app/services/llm_text.py) module exposing extract_json_object() and loads_lenient(), and have studio.py, datatables.py, infographics.py, and mindmaps.py all import from it. Delete the three local copies.

#### 🟠 HIGH · csp chat-completions wrapper (_call_llm_chat) re-implemented 3 times with identical 5-branch exception mapping

- **可信度**:✅ 已驗證 — 投票 3/3(finder confidence=certain)
- **類別**:duplicated logic
- **證據**:_call_llm_chat is defined in anila-studio/app/api/studio.py:741 and duplicated in anila-studio/app/api/infographics.py:307; anila-studio/app/api/datatables.py:282 defines _call_llm whose docstring (:287) explicitly says 'Mirrors studio.py's _call_llm_chat'. Each copy repeats the same five except blocks (CspNotFoundError/CspUnauthorizedError/CspForbiddenError/(CspServerError,CspClientError)) plus the same str(response['choices'][0]['message']['content']) extraction (studio.py:792, infographics.py:340, datatables.py:313). mindmaps.py:93 cross-imports studio.py's copy. The copies even map the same CspNotFoundError differently (datatables → RuntimeError job-fail, infographics/studio → HTTP 503), so a contributor must remember which copy governs which surface.
- **建議**:Move the proxy-call + exception-mapping + content-extraction into one shared helper in app/services (e.g. studio_llm.call_chat(bearer, model, messages, ...) -> str) parameterized on how to surface errors (raise typed StudioLlmError, let each caller translate). Have all four modules call it.

#### 🟠 HIGH · ingestion-worker pipeline (handlers/embedder/judge/evaluator, ~1900 LOC) has effectively no tests

- **可信度**:✅ 已驗證 — 投票 3/3(finder confidence=certain)
- **類別**:insufficient test coverage
- **證據**:ingestion-worker/src has 1965 src LOC across handlers.py (856), evaluator.py (447), judge.py (213), embedder.py (182), but the only test is ingestion-worker/tests/test_uniform_color.py (74 LOC) covering a single image-background helper. The core parse→chunk→embed→pgvector path, the 4096→4000 truncation, the AES-credential judge path, and the SSE job-status writer are untested. This is the data-ingestion backbone shared with CSP via the csp-db.
- **建議**:Add unit tests for embedder truncation/dim-mismatch handling, judge decrypt/skip behavior, and handlers status transitions (with a fake asyncpg pool), plus at least one integration test of ingest_document against a test DB. Target the repo's 80% coverage bar for this package.

#### 🟡 MEDIUM · mindmaps.py reaches into private (underscore) helpers of the 3102-line studio.py module

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=certain)
- **類別**:service coupling
- **證據**:anila-studio/app/api/mindmaps.py:91-96 does `from app.api.studio import SLIDES_LLM_MODEL, _call_llm_chat, _extract_json_object, _loads_lenient`. Importing leading-underscore names across modules couples mindmaps to studio.py's private surface, and forces a full import of studio.py (3102 lines, anila-studio/app/api/studio.py) — including its module-level constants and FluxImageProvider singleton scaffolding — just to reuse three text helpers. Any refactor of studio.py's privates silently breaks mindmaps.
- **建議**:Relocate the shared constants/helpers (SLIDES_LLM_MODEL, JSON helpers, chat helper) into a dedicated app/services module that both studio.py and mindmaps.py import. Treat underscore-prefixed names as module-private and never import them cross-module.

#### 🟡 MEDIUM · studio.py is a 3102-line god-module mixing routing, LLM orchestration, FLUX wiring, JSON parsing, and spec validation

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:architecture
- **證據**:anila-studio/app/api/studio.py is 3102 lines — ~4x the project's own 800-line ceiling (see ~/.claude/rules/common/coding-style.md). It holds the slides HTTP routes, _call_llm_chat, _extract_json_object/_loads_lenient, get_flux_provider() FLUX provider singleton (studio.py:1252), _gated_generate quality-gate loop, _call_llm_for_rebalance, and SlidesSpec validation/correction loop. Other modules import its internals (mindmaps.py:91). The file is the de-facto home for cross-cutting helpers, which is why duplication elsewhere arose.
- **建議**:Split studio.py by concern: routes stay in api/studio.py; move FLUX provider/gating to services/, LLM chat + JSON helpers to a shared services module, and spec validation/correction to services/. Target <800 lines per file per the repo's coding-style rule.

#### 🟡 MEDIUM · Studio defines FLUX/RENDERER settings in config.py but the code reads os.environ directly, bypassing the settings object

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:inconsistent configuration
- **證據**:anila-studio/app/config.py declares FLUX_BACKEND_URL (:30), FLUX_CACHE_DIR (:57), and RENDERER_BASE_URL (:33), but get_flux_provider() reads os.environ.get('FLUX_BACKEND_URL'/'FLUX_CACHE_DIR'/'FLUX_MAX_CONCURRENT'/'FLUX_TIMEOUT_SECONDS') directly (anila-studio/app/api/studio.py:1266,1275,1278,1279) and geometric_qa.py:38 reads os.environ.get('RENDERER_BASE_URL'). A grep shows settings.FLUX_* and settings.RENDERER_BASE_URL are referenced nowhere. FLUX_MAX_CONCURRENT/FLUX_TIMEOUT_SECONDS aren't even in Settings. Two parallel config sources can diverge (e.g. .env loaded by pydantic vs raw env), and the settings fields are dead.
- **建議**:Make Settings the single config source: add FLUX_MAX_CONCURRENT/FLUX_TIMEOUT_SECONDS to Settings and have get_flux_provider() and geometric_qa() read settings.* instead of os.environ. Remove fields that stay unused.

#### 🟡 MEDIUM · anila-studio configures no logging and never wires settings.LOG_LEVEL; studio.py uses print() for diagnostics

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:missing/messy logging
- **證據**:anila-studio/app/main.py creates logger=getLogger(__name__) (:35) but never calls logging.basicConfig()/dictConfig(); settings.LOG_LEVEL exists at config.py:20 but is referenced nowhere in app/. Only myCSPPlatform's main.py configures logging. Consequently studio's many logger.info/debug calls depend entirely on uvicorn's root config and LOG_LEVEL is dead. Separately, studio.py:966 emits validation-failure diagnostics via print(..., flush=True) instead of logger, bypassing levels/formatting. ingestion-worker (ingestion_worker/main.py) and anila-core-router also configure no logging.
- **建議**:Add a logging.basicConfig/dictConfig in each service's startup honoring its LOG_LEVEL setting (or a shared logging-setup module). Replace studio.py:966 print() with logger.warning. Ensure log format/level is consistent across csp/studio/worker/router.

#### 🟡 MEDIUM · tiktoken used for token metering in CSP is not declared in requirements — production silently degrades to a rough heuristic

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:unused/missing dependency
- **證據**:myCSPPlatform/backend/app/services/proxy_service.py:262 does `import tiktoken  # type: ignore[import-not-found]` inside a try/except that falls back to a char-class heuristic (proxy_service.py:272-279) on any failure. tiktoken does not appear in myCSPPlatform/backend/requirements.txt or pyproject.toml. In the deployed container the import therefore fails and every token count uses the heuristic, not accurate BPE counts — the precise path is effectively dead code. Token counts feed token_usage metering/billing, so accuracy matters.
- **建議**:Either add tiktoken to requirements.txt so the intended accurate path runs, or, if the heuristic is the deliberate production behavior, remove the tiktoken branch and document that token counts are estimates. Don't ship a metering path that silently never executes.

#### 🟡 MEDIUM · anila-core-router (token bootstrap + LLM routing) has zero tests

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:insufficient test coverage
- **證據**:anila-core-router/main.py is 358 LOC and contains the 3-priority service-token resolution, state-file persistence, primary-LLM resolution with TTL cache, and the 503 gate on /v1/chat/completions. The find for test files under anila-core-router returns 0. A regression in token resolution or the primary-model gate would ship undetected.
- **建議**:Add tests covering _load_service_token priority order, _initialise_token_source state transitions (legacy env vs state-file vs bootstrap vs none), and the /v1/chat/completions 503 gate when no primary is resolved.

#### 🟡 MEDIUM · Ingestion job-status DB write swallows all exceptions with no log line

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:inconsistent error handling
- **證據**:ingestion-worker/src/ingestion_worker/handlers.py:633-637: the _update_job helper runs an UPDATE ingestion_jobs ... and on any exception does `except Exception: pass` with no logging. The docstring (:606) calls it best-effort, but a systematic failure (schema drift, RLS denial, pool exhaustion) leaves job status/progress silently desynced from reality with zero operator signal — and the SSE stream driven by these rows would stall without trace. This violates the repo's 'never silently swallow errors' rule.
- **建議**:Keep the best-effort semantics but log at warning/debug inside the except (e.g. logger.warning('job-status update failed for %s', arq_job_id, exc_info=True)) so failures are observable.

#### ⚪ LOW · ingestion-worker settings docstring states 1536-d / vector(1536) but code and DB schema use 4000-d / halfvec(4000)

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:api contract drift
- **證據**:ingestion-worker/src/ingestion_worker/settings.py docstring lines 13-15 say 'Sprint 1 pins the output dim to 1536 ... must match the vector(1536) column', but the actual embedding_dim Field default is 4000 (settings.py:53) with a description citing halfvec(4000)/migration 0015. The two statements in the same file contradict each other and the live schema; a maintainer trusting the docstring would mis-size the embedding contract that crosses into csp-db.
- **建議**:Update the module docstring to reflect the current halfvec(4000) contract (and the 4096→4000 NV-embed-V2 truncation), or delete the stale 1536 reference.

#### ⚪ LOW · docker-compose advertises ALGORITHM: HS256 for csp, but csp hard-codes RS256 and never reads settings.ALGORITHM

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:dead code / misleading config
- **證據**:docker-compose.yml sets `ALGORITHM: HS256` for the csp service, and myCSPPlatform/backend/app/config.py:18 keeps `ALGORITHM: str = 'HS256'  # Legacy`. But myCSPPlatform/backend/app/utils/security.py:45 hard-codes `ALGORITHM = 'RS256'` and uses it for encode/decode (security.py:217,231,264); settings.ALGORITHM is read nowhere in app/. anila-studio/app/auth.py correctly verifies with RS256/JWKS. The env var and config default are dead and actively misleading about the auth algorithm.
- **建議**:Remove the ALGORITHM env from docker-compose.yml and the unused config.py:18 field (or comment it as wire-format-only), so the only source of truth for the JWT algorithm is utils/security.py.

#### ⚪ LOW · python-docx declared as a studio dependency but never imported

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=high)
- **類別**:unused/missing dependency
- **證據**:anila-studio/pyproject.toml declares `python-docx>=1.1` with the comment '讀 DOCX 輸出做最終 sanity check', but no `import docx` / `from docx` / `Document(` appears anywhere under anila-studio/app (the DOCX path in services/report_renderer.py uses pypandoc, lazily imported at :277, not python-docx). The dependency adds image footprint without use.
- **建議**:Remove python-docx from pyproject.toml, or implement the intended DOCX sanity-check that justifies it. (Note: pypandoc IS used via lazy import, so keep it.)

#### ⚪ LOW · anila-core-router runs blocking bootstrap (new event loop) at module import time

- **可信度**:✅ 已驗證 — 投票 1/1(finder confidence=medium)
- **類別**:architecture
- **證據**:anila-core-router/main.py:213 calls _initialise_token_source() at module top-level, which under the CSP_BOOTSTRAP_TOKEN branch creates a fresh asyncio event loop and run_until_complete(_self_bootstrap()) (main.py:191-195) during import. Import-time side effects with network/event-loop work make the module hard to test, import, or reason about, and can race with the ASGI server's own loop setup.
- **建議**:Move token initialization into a FastAPI lifespan/startup handler (async) instead of running it at import, so importing the module is side-effect-free and the work shares the app event loop.


## 附錄 C:救回 findings(45 條,verifier 因工具故障未跑完;critical/high 已由人工程式碼驗證)


### 新功能(17)

#### 🟠 HIGH · Classification does not propagate from conversations to Studio-generated documents (reports/infographics/mindmaps/datatables/PPTX)

- **可信度**:✅ 人工程式碼驗證(finder confidence=high)
- **類別**:Document classification / data spillage
- **證據**:The platform already enforces a one-way classification latch on conversations (myCSPPlatform/backend/app/models/conversation.py:31-41 `classified`/`classified_at`/`classified_by`/`classification_inherited`; migration 0031_conversation_classification_inherited.py documents the Bell-LaPadula 'no write down' intent). But the Studio generation path that turns knowledge-base content into exportable artifacts carries no classification at all: ReportSpec (anila-studio/app/schemas/report.py:116-167) has only `preset`/`title`/`sections` and the renderer (anila-studio/app/services/report_renderer.py:179-205 `render_html`) passes only `spec`, `sections_with_html`, `preset_label` to the template. The base report template (anila-studio/app/templates/report/base.html.j2:151-212) renders a footer with the preset label and generated_at but no classification marking. `grep` for classification/watermark across anila-studio/app/services/*job_service.py and templates returns nothing. A user can ask Studio to summarize a classified knowledge base and the resulting PDF/DOCX/PPTX is born unclassified and unmarked.
- **建議**:Add a `classification` field (enum: 普通/機密/極機密 mapped to ROC 國軍 密等) to ReportSpec/InfographicSpec/MindmapSpec/DatatableSpec and propagate from the originating collection or conversation. Have the renderers stamp the classification on every page header/footer (and the PPTX skill on each slide). Default-fail closed: if source classification is unknown, mark as the highest plausible level rather than blank.

#### 🟠 HIGH · Source documents and knowledge-base collections have no classification/sensitivity level

- **可信度**:✅ 人工程式碼驗證(finder confidence=high)
- **類別**:Document classification
- **證據**:IngestionCollection (myCSPPlatform/backend/app/models/ingestion.py:48-95) and IngestionDocument (ingestion.py:102-145) define name/description/status/sha256/storage_path etc. but no classification,密級, or clearance column. grep for classification/sensitive/clearance/機密 across migrations 0014_add_ingestion_platform.py and 0019_collection_first_class.py returns nothing. Classification today can only be derived after the fact at the conversation level (agent `requires_encryption` in models/agent.py:53 or memory recall inheritance). This means an uploaded SECRET PDF sitting in a collection is indistinguishable from a public one until it happens to be pulled into a classified agent's chat.
- **建議**:Add a `classification_level` column to IngestionCollection and IngestionDocument (with an enforced ordering), set it at upload time, and make the conversation classification latch inherit the MAX classification of any chunk it retrieves (extend the existing `classification_inherited` mechanism to read from document level, not just agent `requires_encryption`). Gate retrieval so a user below a document's clearance cannot pull its chunks.

#### 🟡 MEDIUM · No prompt version management or central prompt store

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Prompt version management
- **證據**:Prompts are flat static markdown files (anila-agent/anila_agent/prompts/system.md, agent.md, tool_policy.md) loaded via SystemContextBuilder.from_markdown (anila-agent/anila_agent/prompts/prompt_builder.py:195). The 36 backend migrations contain no prompt/prompt_template/prompt_version table (grep over myCSPPlatform/backend/app/models found zero matches for prompt_version/PromptVersion/prompt_template), and registered agents only persist an endpoint_url + description_for_router (myCSPPlatform/backend/app/models/agent.py:42-44) — the platform never stores the agents' actual system prompts, so there is no versioning, diff, rollback, or audit of prompt changes. Mature platforms (LangSmith Prompt Hub, Vellum, Dify) treat prompts as first-class versioned, taggable artifacts.
- **建議**:Add a prompt_versions table (prompt_id, version, content, label/tag, created_by, created_at, parent_version) plus CRUD endpoints, and let the prompt_builder / studio renderers resolve a prompt by id+label rather than reading a fixed .md. Surface version history and rollback in the control-panel UI.

#### 🟡 MEDIUM · No A/B prompt comparison / experiment framework

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:A/B prompt comparison
- **證據**:Repo-wide search for a/b test, experiment, variant, champion/challenger over *.py/*.vue/*.ts (excluding .venv/node_modules) returned only unrelated 'invariant'/'variant' usages — no experiment, traffic-split, or side-by-side prompt-comparison construct exists. The chunking evaluator (myCSPPlatform/backend/app/api/ingestion/eval_runs.py) compares chunking strategies on Hit@k/MRR but has no analogue for comparing two prompt versions or two model configs on the same input set. Mature platforms (LangSmith/Vellum/Dify) offer side-by-side prompt diffing and A/B evaluation runs.
- **建議**:Build on the existing eval-run machinery: add an experiment entity that pins two or more (prompt_version, model) arms, replays a shared dataset through each, and reports per-arm scores side by side in the UI.

#### 🟡 MEDIUM · Generic answer-quality eval / reusable datasets are absent (eval is chunking-retrieval only)

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Eval / datasets
- **證據**:The only eval subsystem is the ingestion chunking evaluator: IngestionEvalRun scores chunking strategies via Hit@k/MRR with an optional LLM-as-judge on retrieval top-k (myCSPPlatform/backend/app/api/ingestion/eval_runs.py:1-25, 63-114). Its golden set is {query, expected_doc_id} pairs scoped to one collection (eval_runs.py:70-74), and datasets are inline in the request body — not a reusable, versioned dataset entity. There is no end-to-end agent/RAG answer evaluation (faithfulness, answer relevance, golden-answer match): grep for golden/ground-truth/faithfulness/ragas across backend + anila-agent matched only the chunking eval_runs.py docstring. Mature platforms (LangSmith datasets+evaluators, LlamaIndex evaluation, Vellum test suites) treat datasets and answer-quality metrics as first-class, reusable across runs.
- **建議**:Introduce a first-class dataset entity (rows of input + expected output/reference, versioned, reusable across runs) and a pluggable evaluator layer (exact/semantic match, LLM-judge faithfulness/relevance) that scores full agent/RAG answers, not just retrieval hits. Reuse the existing async eval-run/worker pattern.

#### 🟡 MEDIUM · No tool-use trace viewer UI; agent traces only land in local JSONL/stdout

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Tool-use trace UI
- **證據**:anila-agent has a full span/trace tracer (anila-agent/anila_agent/tracing/tracer.py:46) but its only processors write JSONL files or stdout (anila-agent/anila_agent/tracing/processor.py:46 JsonlTracingProcessor, :97 ConsoleTracingProcessor) — there is no processor that ships spans to the backend. The backend mounts no trace-ingestion router (myCSPPlatform/backend/app/api/router.py:31-57 has no trace/span router; grep for trace/span endpoints in app/api found only incidental trace_id/traceback comments). trace_id is stored on TokenUsage (myCSPPlatform/backend/app/models/token_usage.py:25) but no frontend view renders a span tree / waterfall (myCSPPlatform/frontend/src/views has no trace/timeline view; the only 'span' hits are HTML <span> tags). Mature platforms (LangSmith run tree, Dify tracing) provide an interactive nested trace/tool-call timeline.
- **建議**:Add a span-ingestion endpoint + storage table on the backend, ship a TracingProcessor from anila-agent that POSTs spans (correlated by the existing trace_id), and build a run-tree/waterfall view in the control panel keyed off trace_id so operators can inspect tool calls, sub-agent dispatch, and latency per span.

#### 🟡 MEDIUM · Org-level quota/budget enforcement is intentionally absent at the gateway; agent-side limits engine is not wired in

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Cost tracking & quotas
- **證據**:Cost/usage *tracking* is strong: TokenUsage records prompt/completion/total tokens, department_id, model_id, trace_id (myCSPPlatform/backend/app/models/token_usage.py:14-25), and rich analytics endpoints exist (myCSPPlatform/backend/app/api/usage.py: summary/chart/top-models/top-users/top-departments/top-agents/export). But *enforcement* was deliberately removed: migration 0006_drop_quota_rate_limit.py:1-12 drops the quota_policies table and per-user/per-key quota FKs, with rationale 'on-prem local model → no quota'. proxy_service.py (the LLM gateway) has no quota/budget/429 check (grep found no quota/budget/limit enforcement). anila-agent has a LimitsEngine (anila-agent/anila_agent/core/policy_limits.py:1) for per-user/department/agent token & USD caps, but grep shows it is not imported anywhere in myCSPPlatform or anila-studio — it is a standalone unused module. So spend is observable but not capped per tenant. Note the on-prem-no-API-cost rationale is by design; flagging the enforcement gap for environments wanting hard caps.
- **建議**:If hard caps are desired (e.g. multi-team on-prem fairness or BYO-key cost control), wire the existing anila-agent LimitsEngine (or re-introduce a quota model) into proxy_service so requests over a department/user token/USD budget return 429, reusing the already-tracked TokenUsage totals.

#### 🟡 MEDIUM · Audit log is not tamper-evident or append-only and has no retention policy

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Compliance audit logging
- **證據**:AuditLog (myCSPPlatform/backend/app/models/audit_log.py:7-23) is an ordinary mutable table with an autoincrement id and no hash chain, sequence-integrity, prev-hash, or signature column. audit_service.log_audit_event (services/audit_service.py:7-35) just `db.add`s a row. grep for retention/hash_chain/prev_hash/append-only/immutable/integrity across the audit model, service, and API returns nothing. The API (api/audit_logs.py:38-57) is read+filter only with no export. For an NCSIST/國軍 deployment, an attacker (or admin) with DB write access can silently delete or edit audit rows including the `access_classified_conversation` and `classify_conversation` events, defeating the whole point of the classified-access trail.
- **建議**:Add a monotonic per-row hash chain (each row stores prev_row_hash + row_hash over canonical fields) so tampering is detectable, enforce append-only at the DB layer (revoke UPDATE/DELETE on audit_logs from the app role; use a trigger), and add a configurable retention + WORM export (signed JSONL to an offline store). The prod-intranet-card branch already added an ISO-42001 traceability migration (0035_iso_42001_traceability.py) — align this with that work rather than duplicating.

#### 🟡 MEDIUM · Department model is flat — no unit/department hierarchy tree for org-scoped permissions

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Unit/department permission tree
- **證據**:Department (myCSPPlatform/backend/app/models/department.py:7-21) has only id/name/description/is_active with no parent_id or path. grep for parent/parent_id/parent_department/hierarchy/tree/ltree across migrations 0001 and the whole backend/app returns nothing relevant. Access control (services/access_control.py:46-63 `_active_link_ids_for_user`) grants are per-user OR a single flat `department_id` match — there is no notion of a parent unit automatically covering its sub-units. A 國軍 org chart (司令部 → 指揮部 → 大隊 → 中隊) cannot be modeled, so an admin must re-grant every leaf department individually and cannot express 'this resource is visible to the whole command and everything under it'.
- **建議**:Add `parent_id` (self-FK) to departments plus a materialized path or closure table, and extend access_control to resolve a user's effective department set to include ancestor grants. Keep the change additive (NULL parent = top-level) so existing flat departments keep working.

#### 🟡 MEDIUM · No release/approval workflow for Studio-generated deliverables

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:Approval workflows
- **證據**:The only approval state in the system is agent registration (myCSPPlatform/backend/app/models/agent.py:50 `approval_status` pending/approved/rejected with approved_by/approved_at). Studio job services (anila-studio/app/services/report_job_service.py, studio_job_service.py) move jobs straight from generated to downloadable; download_report (anila-studio/app/api/reports.py:130-202) streams the artifact to any authenticated owner with no review/sign-off (簽核) gate. grep for review/release/簽核/核准/送審 in anila-studio/app returns only template/preset names, not a workflow. For 國軍 deliverables, generated reports/briefings typically require a second-person review before they can be exported or shared externally.
- **建議**:Introduce an optional artifact approval state (draft → pending_review → approved → released) keyed on classification level: artifacts above a configurable 密等 cannot be downloaded or share-linked until a reviewer in the same/parent unit approves. Reuse the existing AuditLog to record each approval transition.

#### ⚪ LOW · No semantic / response cache for LLM calls

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Semantic cache
- **證據**:The component named 'prompt_cache' (anila-agent/anila_agent/core/prompt_cache.py) is a vLLM prefix-cache (KV cache) optimizer that builds byte-identical message prefixes so vLLM --enable-prefix-caching hits — it is explicitly not a semantic response cache (module docstring lines 1-30). proxy_service.py, the LLM gateway, forwards every request to the model backend with retry/timeout and has no response-cache lookup (header docstring proxy_service.py:1; grep for semantic_cache/response_cache returned no code matches). Redis is present but used only for token-revocation pub/sub (myCSPPlatform/backend/app/services/token_revocation_publisher.py:1), not response caching. Mature platforms (Dify, Vellum) offer optional semantic/exact response caching to cut latency and repeated-query cost.
- **建議**:Add an optional embedding-keyed semantic cache (or at minimum an exact-prompt-hash cache) in proxy_service in front of the model backend, with per-collection/per-agent TTL and a bypass flag; reuse the existing pgvector + Redis infrastructure.

#### ⚪ LOW · Agent registry has no discovery/marketplace surface and no agent versioning

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:Agent registry / marketplace
- **證據**:A registry exists with register/approve/reject/health-check and a downloadable template (myCSPPlatform/backend/app/api/agents.py:205 download_template, :229 register, :401 approve, :534 health-check), and the Agent model carries capabilities + description_for_router + approval_status (myCSPPlatform/backend/app/models/agent.py:44-50). However list_agents only returns agents the caller owns (or all, for admins) — non-owner non-admin users cannot browse/discover other teams' approved agents (agents.py:286-299), so there is no shared catalog/marketplace. The Agent model has no version column (agent.py:32-109), so registered agents cannot be versioned/pinned. Mature platforms (Dify app store, Vellum) provide a browsable catalog of shareable, versioned agents/apps.
- **建議**:Add a discoverable catalog endpoint that lists approved agents to all authenticated users (respecting role/department gates), plus an agent_version concept so consumers can pin a known-good revision; optionally add ratings/usage stats already available from the usage service.

#### ⚪ LOW · Multi-tenancy is single-level departments with grant-based access, not isolated orgs

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:Multi-tenant org management
- **證據**:The tenant unit is a flat Department (myCSPPlatform/backend/app/models/department.py:7-21: id/name/description/is_active only — no parent, no per-org settings/limits). Access is mediated by role + per-user/per-department ServiceAccessGrant with default-deny (myCSPPlatform/backend/app/services/access_control.py:8-26, 46-54), and admins bypass all gates (access_control.py:67-71). There is no nested organization hierarchy, no per-tenant DB row-level-security isolation, and no per-org configuration/quota object. Knowledge collections and agents are owner-scoped + admin-global rather than tenant-isolated. This suits a single-organization on-prem deployment but lacks the org/workspace boundary that SaaS RAG platforms (Dify workspaces, LangSmith orgs) provide.
- **建議**:If true multi-org/SaaS isolation is ever required, introduce an organization entity above departments, scope agents/collections/usage by org_id, and enforce isolation (Postgres RLS or query-level org filters) so admins of one org cannot see another org's data. For the current single-org on-prem target this is nice-to-have.

#### ⚪ LOW · No military/government document template library — report presets are generic business voices

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Military-specific template libraries
- **證據**:Report presets are DEEP_TECH_REVIEW / KEY_SUMMARY / TEACHING_HANDOUT / EXTERNAL_COMMS (anila-studio/app/schemas/report.py:62, report_renderer.py:72-83 `_PRESET_TEMPLATE`/`_PRESET_LABEL`). grep for military/軍/國防/公文/簽呈/MND across anila-studio/app/templates and report.py returns nothing. There are no ROC government document formats (公文/簽呈/情資報告/作戰指令/會議紀錄) which are the actual deliverable formats for the 國軍 delivery line. The renderer comment at report_renderer.py:58 notes a new preset is a '3-line change: add the enum, add the template' — the extension point exists but is unused for this domain.
- **建議**:Add a small set of ROC-government-format Jinja2 templates (公文三段式: 主旨/說明/辦法; 簽呈; 情資/態勢報告) as new ReportPreset enum values + template files, including the classification header/footer block from the first finding. This is the highest-leverage, lowest-risk ANILA-specific addition given the lean main-branch baseline targets 國軍 delivery.

#### ⚪ LOW · No offline / air-gapped agent profile concept for intranet-only deployment

- **可信度**:⚠️ 待複驗(finder confidence=low)
- **類別**:Offline agent profiles
- **證據**:Agent runtime_config (myCSPPlatform/backend/app/models/agent.py:74-98) documents a `workspace.allow_network` boolean but it is a free-form JSON hint with no enforced 'offline profile' that disables all egress (MCP servers, external endpoints, FLUX, web tools) as a unit. grep for offline/air-gap/斷網/離線 in anila-agent/anila_agent returns only MCP connect/disconnect lifecycle code, not an offline mode. For an NCSIST intranet (no internet) the operator must hand-tune deny_lists per agent rather than selecting a vetted 'air-gapped' profile that guarantees no outbound calls.
- **建議**:Define a named, enforced agent profile (e.g. `network_profile: airgapped|intranet|open`) that, when set to airgapped, hard-blocks MCP egress, external LLM/embedding endpoints, and FLUX/web tools at the runtime layer regardless of per-tool allow_lists. Surface it in the admin control panel alongside the existing runtime_config knobs.

#### ⚪ LOW · Cross-collection / org-network DNS resolution relies on a single shared external Docker network with no per-environment DNS override layer

- **可信度**:⚠️ 待複驗(finder confidence=low)
- **類別**:Government-network DNS overrides
- **證據**:docker-compose.yml:102-110 wires csp to model services purely by Docker service name on the shared `anila-models-net` external network (`gemma4`/`gpt-oss-20b`/`nv-embed-proxy`) plus a single `host.docker.internal:host-gateway` extra_host. There is no `dns:`/`dns_search:` configuration and only one hard-coded `extra_hosts` entry (a second internal host `gitlab.anila.internal` appears as a static hostname at docker-compose.yml:417). For NCSIST intranet deployment, internal DNS names (e.g. on-prem 國軍 DNS, internal CA/OCSP, mail relays) cannot be injected per environment without editing compose files.
- **建議**:Introduce an env-driven DNS/extra_hosts override block (e.g. `DNS_SERVERS`, `DNS_SEARCH`, and an `extra_hosts` list sourced from .env) applied to the csp/studio/agent services so the intranet line can point at government DNS and internal CA/OCSP responders without forking the compose file. Document it next to the existing anila-models-net bootstrap note.

#### ℹ️ INFO · Cross-session long-term memory IS implemented (no gap) — noted to avoid false flag

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Cross-session long-term memory
- **證據**:Contrary to a typical platform gap, ANILA implements per-user cross-conversation long-term memory: UserFact stores structured long-term facts upserted on (user_id, key) after each turn, and ConversationMemoryChunk stores per-message halfvec(4000) embeddings for cross-conversation semantic recall (myCSPPlatform/backend/app/models/user_memory.py:1-11, 41-52, 93-128). memory_service.retrieve_relevant_chunks / build_memory_block perform cross-conversation RAG recall excluding the active conversation (myCSPPlatform/backend/app/services/memory_service.py:152-216, 274-283), and anila-agent additionally has a Memdir-backed LongTermMemory recall (anila-agent/anila_agent/memory/long_term.py:1-12). This capability is present and reasonably mature.
- **建議**:No action needed. Optionally expose a user-facing 'what the assistant remembers' management UI and memory TTL/forget controls to match Dify/LangSmith memory-management UX.


### Dev 體驗(14)

#### 🔴 CRITICAL · Router dispatch sends X-CSP-Service-Token + X-ANILA-User-* headers, NOT the user JWT the agent expects — every production dispatch would 401

- **可信度**:✅ 人工程式碼驗證(finder confidence=certain)
- **類別**:auth-model / wrong-mental-model
- **證據**:The reference impl's central architecture (militarylaw-agent/BUILDING_THIS_AGENT.md:230-279, §6 'csp Router dispatch propagate 使用者的 JWT') and militarylaw-agent/service.py:87-97 (`_extract_bearer` requires `Authorization: Bearer <jwt>` and 401s otherwise) assume csp forwards the caller JWT. But the actual csp proxy builds downstream headers in myCSPPlatform/backend/app/services/proxy_service.py:121-145 (`_build_downstream_headers`): it sets `X-CSP-Service-Token` (per-agent service token) + `X-ANILA-User-Id`/`-Email`/`-Groups` only — there is no `Authorization` header. The dispatch call sites confirm this (myCSPPlatform/backend/app/api/proxy.py:476 stream, :521 non-stream — both go through `_build_downstream_headers`/`inject_identity`). So a Router-dispatched request arrives with NO bearer, and `service.py:_extract_bearer` 401s on every real dispatch. The pattern only works for dev curl where the human manually adds a JWT.
- **建議**:Fix the onboarding story: a Router-dispatched agent receives `X-CSP-Service-Token` + `X-ANILA-User-*` identity headers, not the user's JWT. The agent cannot act 'on behalf of the user' against the JWT-only search API using what the Router gives it. Document the real options: (a) the agent mints/holds its own credential to call search, or (b) csp adds a collection-search path that accepts the service token + `X-ANILA-User-Id`. Update service.py to read identity from `X-ANILA-User-*` and authenticate to search with a real accepted credential.

#### 🔴 CRITICAL · The search API (control plane) accepts only a JWT or cookie — it rejects the X-CSP-Service-Token the Router actually hands the agent

- **可信度**:✅ 人工程式碼驗證(finder confidence=certain)
- **類別**:auth-model / cross-service contract
- **證據**:`POST /api/ingestion/collections/{id}/search` depends on `get_current_user` (myCSPPlatform/backend/app/api/ingestion/search.py:242). `get_current_user` resolves a token only from the `Authorization` header or the `anila_access_token` cookie and then `decode_token` (myCSPPlatform/backend/app/services/auth_service.py:89-111) — it has no path for `X-CSP-Service-Token` or `X-ANILA-User-*`. Service tokens are validated by a separate dependency `verify_service_token` (auth_service.py:168+) which the search route does not use. So even if the agent forwarded exactly what the Router gave it, the search call would 401. BUILDING_THIS_AGENT.md:115-120 (坑 1) correctly says 'control plane = JWT, sk- key 401s' but then wrongly concludes the Router supplies that JWT.
- **建議**:State explicitly that the search endpoint is JWT/cookie-only and is not reachable with a service token. A general dev wiring an HTTP retriever needs a genuine JWT at call time; clarify where that JWT comes from in production (it is NOT the Router dispatch headers). Consider this a platform gap to resolve before the 'wrap as service + register in Router' path is viable.

#### 🟠 HIGH · A general dev can only search/list collections they personally created (created_by) or be admin — the id=54 example silently assumes ownership/admin

- **可信度**:✅ 人工程式碼驗證(finder confidence=certain)
- **類別**:privilege / undocumented blocker
- **證據**:`_require_collection_access` (myCSPPlatform/backend/app/api/ingestion/collections.py:44-72) grants access only when `is_admin_tier(user)` OR `coll.created_by == user.id`; otherwise 403 'No access to collection {id}'. The list endpoint (collections.py:150-180) defaults to `owned_only=True` and rejects `owned_only=false` for non-admins with 403. The reference doc (BUILDING_THIS_AGENT.md:55-60) tells the dev to just look up 'collection id=54' and points the agent at it, but an external dev who did not create collection 54 (and is not admin) gets 403 on every search. 'No db access' the prompt assumes is correct, but 'has a csp account' is NOT sufficient to read someone else's collection.
- **建議**:Add a prerequisite to onboarding: you can only retrieve from a collection you own (created_by) or one you are admin over. There is no `collection_access_grants` sharing yet (collections.py:50-53 marks it as future work). An external dev must either own the target collection or get an admin to share/create it. Replace the bare 'find id=54' step with 'confirm the collection is one you created via GET /api/ingestion/collections'.

#### 🟠 HIGH · Registering the agent endpoint is blocked by an owner-only trusted-host step that a general dev cannot perform

- **可信度**:✅ 人工程式碼驗證(finder confidence=high)
- **類別**:privilege / wrong-ordering of blockers
- **證據**:BUILDING_THIS_AGENT.md:278 says registration's 'SSRF guard 會要你把 agent FQDN 加進 /trusted-hosts' as if the dev can do it. But the SSRF guard (anila-core/src/anila_core/security/url_guard.py:234-326) rejects private RFC1918 IPs (e.g. the 172.16.120.x segment in the example .env) unless `ANILA_ALLOW_PRIVATE_ENDPOINT=1` (a compose env, docker-compose.yml:53, not user-facing) or the host is in the trusted-hosts allow-list. Adding to that allow-list is owner-only: `POST /api/trusted-hosts` depends on `require_owner` (myCSPPlatform/backend/app/api/trusted_hosts.py:48-52), the highest tier. A general dev (or even a plain admin) cannot add it. Single-label/docker names are also hard-blocked (url_guard.py:319).
- **建議**:Document that clearing the SSRF guard for a cross-segment/private agent endpoint requires an OWNER (not the dev, not admin) to either add the host via POST /api/trusted-hosts or set ANILA_ALLOW_PRIVATE_ENDPOINT=1 in compose. Re-order the onboarding blockers so this owner dependency is surfaced before the dev builds/deploys, not discovered at registration time.

#### 🟠 HIGH · Examples and README do not cover the real ANILA deployment topology (cross-segment + https + JWT + Router dispatch)

- **可信度**:✅ 人工程式碼驗證(finder confidence=high)
- **類別**:documentation
- **證據**:anila-agent/README.en.md and the three examples (anila-agent/examples/basic_chat.py, rag_agent.py, custom_tool.py) describe only a flat single-endpoint setup: one OpenAI-compatible ANILA_BASE_URL with a static ANILA_API_KEY (.env.example:3-5 `ANILA_BASE_URL=http://localhost:8000/v1`, `ANILA_API_KEY=sk-local`). The real platform topology documented in the root README (/home/aia/c1147259/ANILA/README.md:12-17,51-83,155-165) is far richer: a Router on :9000 dispatching to per-agent endpoint_url over internal https, a CSP control plane issuing JWT (JWKS) / per-credential service tokens, an nginx https gateway, and SSE chunk-forwarding. None of this — cross-segment routing, https, JWT/Bearer caller tokens, or Router dispatch — appears anywhere in anila-agent README/examples. A developer who clones the template gets zero guidance on how their agent fits into the dispatch chain. grep for `JWT|Bearer|Router|dispatch|https://` across README.en.md/README.md returned only the upstream openai-agents github URL.
- **建議**:Add a 'Deployment topology' section to anila-agent/README.{md,en.md} that shows the production path (UI/SDK → nginx https → Router :9000 → CSP JWT/JWKS → agent endpoint_url SSE) and contrasts it with the local single-endpoint dev mode the examples use. Cross-link the root README's topology diagram so template consumers understand the static-key examples are dev-only.

#### 🟠 HIGH · No documentation on how a cloned agent is served as the HTTP/SSE endpoint that the Router dispatches to

- **可信度**:✅ 人工程式碼驗證(finder confidence=high)
- **類別**:documentation
- **證據**:The Router forwards a chosen request to the agent's `endpoint_url` and streams its SSE back (anila-core-router/README.en.md:15,137,141 — e.g. `endpoint_url: http://flux2-dev-agent:8000`). But anila-agent ships only one entry point, the REPL: pyproject.toml:36-37 `anila = "anila_agent.main:main"`, and README.en.md:129-133 documents only `anila` / `anila --prompt`. There is no inbound HTTP server exposing `/v1/chat/completions` SSE: anila_agent/mcp/server.py is an MCP *client* (connects OUT), and anila_agent/runtime/__init__.py:1-19 states the runtime layer is an outbound LLM connection abstraction, explicitly not a server. So a developer cloning the 'official platform template' has no documented or code path to make their agent dispatchable by the Router.
- **建議**:Either ship a documented server entry point (FastAPI/uvicorn serving OpenAI-compatible `/v1/chat/completions` with SSE) and document `anila serve`, or add a doc section explaining exactly how to wrap AnilaRunner in an SSE endpoint and register its `endpoint_url` with CSP. Without this, the 'official sub-agent template' cannot actually be deployed behind the Router as advertised.

#### 🟡 MEDIUM · Agent registration lands in approval_status='pending' and requires an admin /approve before the Router will route to it

- **可信度**:⚠️ 待複驗(finder confidence=certain)
- **類別**:undocumented blocker / wrong-ordering
- **證據**:`register_agent` sets `approval_status='pending'` (myCSPPlatform/backend/app/api/agents.py:273) and `/{agent_id}/approve` depends on `require_admin` (agents.py:401-413). The reference doc §7 (BUILDING_THIS_AGENT.md:278-279) presents 'register → Router dispatch' as if registration is sufficient; it never mentions the pending→approved admin gate. A dev who registers and tests will see the Router not dispatch until an admin approves. Note registration itself does work for a `developer` role (agents.py:172-179 `_require_developer_or_admin` accepts admin/developer/owner), which is more permissive than the collection-access gate.
- **建議**:Add the approval step explicitly: after POST /api/agents/register the agent is `pending`; an admin must POST /api/agents/{id}/approve (and the health check passes) before the Router routes to it. Also note that registration requires at least the `developer` role, not just any csp account.

#### 🟡 MEDIUM · csp_http.py reads CSP_API_KEY from env, but the documented/production model never sets it — from_env() is dead in the Router path and contradicts the per-request-JWT story

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:confusing-terminology / inconsistent-config
- **證據**:militarylaw-agent/anila_agent/retrieval/csp_http.py:117-150 `from_env()` requires `CSP_BASE_URL`+`CSP_API_KEY`+`ANILA_COLLECTION_ID` and raises on a half-config. But militarylaw-agent/.env:31 leaves `CSP_API_KEY` commented out, and service.py:133-138 never calls `from_env()` — it constructs `CspHttpRetriever(api_key=bearer)` per request from the (non-existent in prod) Authorization header. So `from_env()` is only reachable in a dev/manual-JWT-in-CSP_API_KEY mode. The docstring (csp_http.py:6-8) also says `Authorization: Bearer <api-key>` and the module env comment calls it 'sk-... 平台 API key' (csp_http.py:16-17), but the search endpoint rejects sk- keys (only JWT works) — mixing 'API key' and 'JWT' terminology for the same field is a documented trap (BUILDING_THIS_AGENT.md:115) yet the code comments still say 'API key'.
- **建議**:Pick one auth credential name and use it consistently. The search endpoint takes a JWT, so the env var and docstrings should say JWT, not 'sk- API key'. Clarify that `from_env()`/`CSP_API_KEY` is a dev-only convenience and is not the production path (which is per-request, and per findings above is currently broken).

#### 🟡 MEDIUM · The shipped template README (the file devs actually download) documents none of the HTTP-retriever / service / Router path — that knowledge exists only in the unshipped reference doc

- **可信度**:⚠️ 待複驗(finder confidence=certain)
- **類別**:undocumented-knowledge / onboarding gap
- **證據**:The template README that gets zipped and served at /template/download (anila-agent/README.md / README.en.md, packaged by myCSPPlatform/backend/app/api/agents.py:205-226) only documents three retrievers: Dummy, generic langchain-pgvector, and ANILA-native pgvector — all DB-direct for the real options (README.en.md:141-145, README.md:144). It never mentions an HTTP/csp-search retriever, wrapping as an OpenAI-compatible service, the JWT vs service-token auth model, or registering into the Router. All of that lives only in militarylaw-agent/BUILDING_THIS_AGENT.md, which is NOT part of the template (it sits in the separate reference repo) and is excluded from the download anyway. An external dev downloading the template is steered toward the pgvector retrievers, which require exactly the DB access (PGVECTOR_URL, embedding endpoint) the prompt says they don't have.
- **建議**:Add an HTTP-retriever + service-wrapper + Router-registration section to the shipped template README (or ship csp_http.py + a service.py skeleton in the template itself, under examples/). The template's recommended path for an external dev should be the HTTP retriever, with the pgvector retrievers clearly labeled 'platform-operator / has-DB-access only' (BUILDING_THIS_AGENT.md:19 already says this, but the shipped README does not).

#### 🟡 MEDIUM · Misconfiguration error messages do not point to the actual remedy (the hypothesized 'HTTP retriever' does not exist)

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:error-messages
- **證據**:The half-config guard messages say only 'Set both or unset both' (anila_agent/retrieval/anila_pgvector.py:216-217 `ANILA_COLLECTION_ID is set but PGVECTOR_URL is missing. Set both or unset both.`; pgvector.py:103-104 same pattern). They never name the alternatives a dev actually has — fall back to DummyRetriever, switch to the langchain Flavour A, or implement the Retriever Protocol (retrieval/base.py:17-32). Critically, there is NO HTTP/remote retriever in the codebase to guide the dev toward: grep for `HttpRetriever|RemoteRetriever|http_retriever` across the repo (excluding venv) found only Dummy/pgvector/anila_pgvector/base. anila_pgvector.py uses httpx only for the embeddings POST (line 136), not document retrieval. So the error message cannot be made to 'guide the dev toward the HTTP retriever' because none exists; the actionable next step is unstated.
- **建議**:Expand each guard message to name the concrete recovery paths, e.g. 'ANILA_COLLECTION_ID is set but PGVECTOR_URL is missing — set PGVECTOR_URL to the platform Postgres DSN, or unset ANILA_COLLECTION_ID to fall back to DummyRetriever. See README §Filling in your project / Retriever options.' If a remote/HTTP retriever against the platform is the intended production path, add it; otherwise stop implying one exists in any doc.

#### 🟡 MEDIUM · ANILA_SSL_VERIFY is documented as a generic TLS switch but only affects the embedding endpoint, not the chat endpoint

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:documentation
- **證據**:README.en.md:219 and README.md:219 document `ANILA_SSL_VERIFY` generically as '`0` to skip TLS verification (self-signed certs only)' with no scoping, and .env.example:31 places it under the retriever block. In code it is consumed only inside anila_pgvector.py: it is read at line 236-237 and passed to the embeddings httpx client at line 136 (`httpx.AsyncClient(verify=self._verify_ssl, ...)`). The chat model path (models/openai_compatible.py build_model → LitellmModel, lines 18-23) has no SSL-verify knob at all. In the documented topology where the agent talks to the Router/CSP over internal https with self-signed certs, a dev who sets ANILA_SSL_VERIFY=0 will still hit TLS verification failures on the chat connection and have no documented way to disable it.
- **建議**:Scope the README/.env wording to say ANILA_SSL_VERIFY controls only the embedding endpoint. Separately, add (and document) a TLS-verify control for the chat/LiteLLM endpoint, or document the LiteLLM/httpx env var that achieves it, so self-signed internal https deployments work end-to-end.

#### 🟡 MEDIUM · No common-pitfalls / troubleshooting doc

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:documentation
- **證據**:find across anila-agent for any `*pitfall*`, `*troubleshoot*`, `*faq*`, `*deploy*` doc returned nothing (only a venv site-package and an unrelated openai-agents example FAQ). docs/ contains only agent-tool-vs-as-tool.md and p2-15-backlog.md. The README has no Troubleshooting section. Common first-run failure modes — retriever half-config raises (anila_pgvector.py:215, pgvector.py:102), the historical FileNotFoundError from the instructions_file path mismatch (CHANGELOG.md:60-61), gemma4 chain-of-thought truncated by max_tokens=1024 (model.yaml settings + CHANGELOG.md:55-57), self-signed TLS, missing [pgvector] extra (pgvector.py:38-41) — are scattered across code/changelog with no single place a dev can consult.
- **建議**:Add anila-agent/docs/troubleshooting.md (or a README section) collecting first-run failure modes and fixes: retriever env half-config, [pgvector] extra not installed, self-signed TLS for chat vs embed, instructions_file path, truncated output / max_tokens, and how to verify which retriever was installed (build_agent logs 'retriever installed: %s' at agent.py:69).

#### ⚪ LOW · build_agent() auto-detects a pgvector retriever from ANILA_COLLECTION_ID and raises on a half-config, fighting the HTTP-retriever path and forcing an env-pop workaround

- **可信度**:⚠️ 待複驗(finder confidence=certain)
- **類別**:confusing-behavior / footgun
- **證據**:build_agent() calls `_anila_pgvector_from_env() or _pgvector_from_env()` (anila-agent/anila_agent/core/agent.py:66-69). The ANILA-native from_env (referenced README.en.md:144 'half-configured deployments fail loud') treats `ANILA_COLLECTION_ID` as a trigger for the DB retriever. An HTTP-retriever deployment that sets `ANILA_COLLECTION_ID` (as militarylaw-agent/.env:21 does) but no `PGVECTOR_URL` triggers a build-time raise, so service.py must pop `ANILA_COLLECTION_ID`/`PGVECTOR_URL` from env before build then set_retriever after (militarylaw-agent/service.py:142-156, documented as 坑 6 in BUILDING_THIS_AGENT.md:301). This is a non-obvious ordering trap: the same env var name means 'use DB retriever' to build_agent but 'collection to HTTP-search' to the HTTP retriever.
- **建議**:Disambiguate the trigger: have build_agent only auto-install the DB retriever when PGVECTOR_URL is present (treat ANILA_COLLECTION_ID alone as insufficient), or add an explicit ANILA_RETRIEVER=http|pgvector|dummy switch. This removes the env-pop dance and the overloaded meaning of ANILA_COLLECTION_ID.

#### ⚪ LOW · Auth examples use a static API key that does not match the platform's JWT/per-credential Bearer model

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:documentation
- **證據**:.env.example:5 and configs/model.yaml ship `ANILA_API_KEY=sk-local` / `api_key_env: ANILA_API_KEY`, and config.py:119-123 resolves a single static key into the chat client. In production the Router authenticates upstream with the caller's Bearer API key and a separate X-CSP-Service-Token (anila-core-router/README.en.md:122-123,140), and CSP issues JWT via JWKS with token revocation (root README.md:43,200). The template never explains how a developer's agent obtains/validates the caller token, nor how the static-key example maps onto the platform's credential model. This is a doc gap rather than a code defect — the static key is legitimately the dev-mode path — so confidence on impact is bounded.
- **建議**:In the deployment-topology doc, state explicitly that ANILA_API_KEY=sk-local is the local-dev shape and that, behind the Router, the agent is reached with a CSP-issued per-credential Bearer/JWT; point at where token validation (if any) should be wired. Mark clearly which env vars are dev-only.


### anila-agent 樣板(14)

#### 🟠 HIGH · build_agent's env auto-detect silently overwrites a manual set_retriever() call — no escape hatch

- **可信度**:✅ 人工程式碼驗證(finder confidence=certain)
- **類別**:API design / footgun
- **證據**:anila_agent/core/agent.py:66-69 unconditionally runs `retriever = _anila_pgvector_from_env() or _pgvector_from_env()` and, if non-None, calls `set_retriever(retriever)`, overwriting whatever was installed before. README.en.md:145,148-149 (Option C) instructs users to call `set_retriever(MyRetriever())` BEFORE constructing the agent, and examples/rag_agent.py:56-58 does exactly that. In an env-configured deployment (PGVECTOR_URL/ANILA_COLLECTION_ID set), the manual retriever is clobbered inside build_agent. build_agent (agent.py:46-52) exposes `extra_tools`/`extra_hooks` but no `retriever=` parameter and no `auto_detect_retriever=False` flag, so there is no clean override.
- **建議**:Add a `retriever: Retriever | None = None` parameter to build_agent. When provided, skip the env auto-detect entirely and use it. Alternatively, only run env auto-detect when the module-level retriever is still the DummyRetriever default (i.e. `get_retriever()` is a DummyRetriever), so a prior explicit set_retriever() wins. Update README Option C and examples/rag_agent.py to reflect whichever precedence is chosen.

#### 🟠 HIGH · ANILA_COLLECTION_ID set without PGVECTOR_URL raises inside build_agent, before any override can run

- **可信度**:✅ 人工程式碼驗證(finder confidence=certain)
- **類別**:API design / fail-loud-too-early
- **證據**:anila_agent/retrieval/anila_pgvector.py:203-218: from_env() treats ANILA_COLLECTION_ID as the activation signal and raises ValueError('ANILA_COLLECTION_ID is set but PGVECTOR_URL is missing') when the URL is absent. This is called eagerly at agent.py:66 during build_agent, so an operator who sets ANILA_COLLECTION_ID (e.g. inherited from a platform env) but intends to inject a custom retriever (the reported CspHttpRetriever case) cannot get past build_agent — the raise happens before set_retriever could ever take effect. The fail-loud is intentional per the docstring, but it fires unconditionally regardless of whether the caller wants the env-driven retriever at all.
- **建議**:Gate the env auto-detect behind the same `retriever`/`auto_detect_retriever` escape hatch from the previous finding: when an explicit retriever is supplied, do not call from_env() at all, so half-configured platform env vars cannot abort assembly. Keep the fail-loud only for the path where the user actually opted into env-based retrieval.

#### 🟡 MEDIUM · litellm 'openai/' model-prefix pitfall is undocumented; default model masks it, custom models (e.g. gemma4) break

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:LLM provider config / documentation
- **證據**:anila_agent/models/openai_compatible.py:18-24 passes the bare config.model straight into LitellmModel(model=..., base_url=..., api_key=...). LitellmModel forwards this to litellm.acompletion (.venv/.../agents/extensions/models/litellm_model.py:216,333 use str(self.model)). litellm infers the provider from the model name: 'gpt-4o-mini' (the configs/model.yaml:8 and .env.example default) is recognized as OpenAI so base_url override works, but an arbitrary OpenAI-compatible model name like the gemma4 referenced in configs/model.yaml:13 or 'Qwen/...' is not provider-tagged and litellm raises 'LLM Provider NOT provided / cannot map model'. The fix (prefix the model with 'openai/') is not documented in README.en.md, .env.example, configs/model.yaml, or docs/.
- **建議**:Document the 'openai/' prefix requirement next to ANILA_MODEL in .env.example, configs/model.yaml, and README.en.md (e.g. 'For non-OpenAI model names served via an OpenAI-compatible endpoint, prefix with openai/ so litellm routes correctly: ANILA_MODEL=openai/gemma4'). Optionally, build_model could auto-prepend 'openai/' when base_url is set and the model has no provider prefix.

#### 🟡 MEDIUM · No request timeout or retry configured for the LLM model call, and no YAML hook to set them

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Resilience / sane defaults
- **證據**:anila_agent/models/openai_compatible.py:18-24 constructs LitellmModel with only model/base_url/api_key — no timeout and no num_retries. build_model_settings (same file, lines 30-44) hard-codes an allowed-keys set that excludes 'timeout', 'num_retries', and 'max_retries', so even a user who adds them under settings: in model.yaml has them silently dropped (comment at line 28: 'Unknown keys are dropped'). A hung or slow self-hosted endpoint (the documented vLLM/gemma4 long-context target) will block the agent loop indefinitely.
- **建議**:Add a sane default request timeout (e.g. 60-120s) and a small num_retries to build_model, and/or expose them via model.yaml (extend the allowed-keys set or pass through a dedicated `timeout`/`num_retries` field). Document the defaults.

#### 🟡 MEDIUM · No Makefile / task runner — install, test, lint, run are hand-typed multi-flag commands

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:dev-acceleration / task-runner
- **證據**:No Makefile/justfile/Taskfile/noxfile exists at the template root (find at /home/aia/c1147259/ANILA/anila-agent returned none). README.md:111-133 documents setup as raw commands a dev must copy and remember: `uv venv && uv pip install -e ".[dev]"`, `uv pip install -e ".[dev,pgvector]"`, `cp .env.example .env`, `anila`, `pytest`. Lint/typecheck (ruff, mypy from pyproject.toml:28-34) have no documented invocation at all. The vendored reference template DOES ship one (templete/openai-agents-python/Makefile defines sync/format/lint/mypy/typecheck targets), so the pattern is known but was not applied to anila-agent itself.
- **建議**:Add a Makefile at /home/aia/c1147259/ANILA/anila-agent/Makefile with `install` (uv venv + editable install with dev,pgvector extras), `test` (pytest), `cov` (pytest --cov=anila_agent), `lint` (ruff check), `fmt` (ruff format), `typecheck` (mypy anila_agent), and `run` (anila). Mirror templete/openai-agents-python/Makefile's structure and reference it from README §Setup. This removes the per-command memorization tax for every dev who clones the template.

#### 🟡 MEDIUM · No interactive `anila init` — first-run setup is manual cp/edit of .env

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:dev-acceleration / onboarding
- **證據**:main.py:15-31 defines only three CLI flags (--session, --config-dir, --prompt); there is no init/scaffold subcommand (grep for init|scaffold|bootstrap across anila_agent/cli and main.py found only an unrelated __init__ in slash_commands.py:208). Onboarding is purely manual: README.md:126-127 instructs `cp .env.example .env` then hand-edit ANILA_BASE_URL/ANILA_API_KEY/ANILA_MODEL. For the platform's primary retriever path a dev must also know to set PGVECTOR_URL + ANILA_COLLECTION_ID (README.md:144, .env.example:27-31), which are commented out by default — a fresh clone silently runs on DummyRetriever with no prompt to wire a real corpus.
- **建議**:Add an `anila init` subcommand in main.py/cli that interactively prompts for base URL, API key, model, and collection id (or pgvector collection name), then writes a populated .env from .env.example. prompt-toolkit is already a core dependency (pyproject.toml:14) so the interactive prompts need no new dependency. This collapses 'clone -> read README -> hand-edit two files -> discover you're on DummyRetriever' into one guided command.

#### 🟡 MEDIUM · No CSP HTTP retriever — cross-segment devs who cannot reach the database directly have no built-in retriever

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:dev-acceleration / retrieval connectivity
- **證據**:The only shipped retrievers require a direct Postgres connection: retrieval/pgvector.py (langchain_postgres.PGVector, needs PGVECTOR_URL) and retrieval/anila_pgvector.py (asyncpg pool to ingestion_collections/document_chunks, anila_pgvector.py:110-181). retrieval/__init__.py:1-4 exports only Document/DummyRetriever/Retriever. Meanwhile the platform already exposes a stable HTTP retrieval primitive: myCSPPlatform/backend/app/api/ingestion/search.py:234-331 implements `POST /api/ingestion/collections/{id}/search` with a typed SearchRequest (query/top_k/min_score/document_ids) and SearchResponse of citation-ready SearchHitOut rows (chunk_id, document_id, filename, content, score, parent_content). The endpoint is auth'd via get_current_user (search.py:242), so a bearer-token-holding agent on another network segment can call it without DB credentials — but the template provides no client for it.
- **建議**:Add retrieval/csp_http.py implementing the Retriever Protocol (retrieval/base.py:18-39) by POSTing to {CSP_BASE_URL}/api/ingestion/collections/{id}/search with a bearer token and mapping SearchHitOut -> Document (id=chunk_id, text=content or parent_content, score, metadata). Add a `from_env()` keyed on e.g. ANILA_CSP_BASE_URL + ANILA_COLLECTION_ID + token, and wire it into the build_agent retriever fallback chain (currently core/agent.py:66 only tries anila_pgvector then pgvector). httpx is already used in anila_pgvector.py:134-143 so no new dependency is needed. This unblocks the documented consumers in search.py:3-4 (AgenticRAG agents) that live off the csp-db network.

#### 🟡 MEDIUM · No OpenAI-compatible service wrapper example — devs must build their own HTTP server to expose the agent

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:dev-acceleration / deployment example
- **證據**:The package only consumes OpenAI-compatible endpoints (models/openai_compatible.py:18-24 wraps LitellmModel as a client); there is no server that exposes the assembled agent as an OpenAI-compatible /v1/chat/completions endpoint. grep for FastAPI / chat/completions across anila_agent/, examples/, tests/, docs/ returned nothing (only the unrelated MCP transport in anila_agent/mcp). examples/ contains only three client-side scripts (basic_chat.py, rag_agent.py, custom_tool.py) that call runner.send() in-process. A dev who wants other services to talk to their agent over HTTP must design the whole wrapper from scratch.
- **建議**:Add examples/openai_service.py: a minimal FastAPI app exposing POST /v1/chat/completions that builds the agent once (build_agent + AnilaRunner as in examples/basic_chat.py:18-20), maps the last user message to runner.send(), and returns an OpenAI ChatCompletion-shaped envelope (optionally streaming). Keep fastapi/uvicorn as an optional `[serve]` extra in pyproject.toml so the core stays lean. This turns 'agent in a REPL' into 'agent any service can call' with a copyable reference.

#### ⚪ LOW · set_retriever's isinstance(Retriever) guard cannot catch sync implementations or wrong signatures

- **可信度**:⚠️ 待複驗(finder confidence=high)
- **類別**:Protocol robustness
- **證據**:anila_agent/retrieval/base.py:17 marks Retriever as @runtime_checkable, and anila_agent/tools/rag_tools.py:22 does `if not isinstance(retriever, Retriever)`. runtime_checkable Protocols only verify method/attribute *presence*, not async-ness or signatures. A retriever with a synchronous `def search(...)` (instead of the required `async def`, base.py:30) passes the isinstance check but then breaks at call time: rag_tools.py:49 does `await _retriever.search(...)`, which raises 'object list can't be used in await expression'. The Protocol docstring (base.py:27) says implementations may be async and the wrappers await everything, but a sync impl is silently accepted at install time and fails only on first query.
- **建議**:In set_retriever, additionally assert inspect.iscoroutinefunction(retriever.search) and retriever.fetch (or wrap sync results via asyncio.to_thread in the tool layer). At minimum, document in base.py that search/fetch MUST be coroutines, since the isinstance guard gives false confidence.

#### ⚪ LOW · read_document built-in tool is effectively dead for both production pgvector retrievers (always returns None)

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:Completeness / tool UX
- **證據**:Both production retrievers hard-return None from fetch(): anila_agent/retrieval/pgvector.py:84-87 and anila_agent/retrieval/anila_pgvector.py:183-185. The read_document tool (anila_agent/tools/rag_tools.py:66-75) is wired as a default built-in (configs/tools.yaml: 'anila_agent.tools.rag_tools.read_document') and exposed to the model, but with either pgvector backend it can only ever return None. The model may waste a turn calling read_document and get nothing useful. This is a documented design choice (chunks carry full content from search), so not a correctness bug, but it ships a built-in tool that no-ops in the two non-toy backends.
- **建議**:Either implement fetch() for the pgvector retrievers (a by-id SELECT on document_chunks is straightforward), or make read_document conditional/opt-in (drop it from the default builtin list for chunk-based backends) and note in the system prompt that search results already carry full chunk text.

#### ⚪ LOW · AnilaPgVectorRetriever uses a single shared asyncpg pool with no per-statement timeout; SSL verify default is sane

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:Resilience / sane defaults
- **證據**:anila_agent/retrieval/anila_pgvector.py:110-115 creates one asyncpg pool (min 1, max 4) with no command_timeout; the embed HTTP call (line 136) uses timeout=30.0 with no retry. A slow/hung DB query in search() (lines 152-168) has no statement timeout and can block. On the positive side, SSL verification defaults to True (lines 236-237, verified by tests/test_anila_pgvector_retriever.py:272-289) with a clean ANILA_SSL_VERIFY opt-out for self-signed certs, and the embed timeout of 30s is reasonable — so the SSL default is well-handled; the gap is DB/query timeout only.
- **建議**:Pass command_timeout to asyncpg.create_pool (e.g. 30s) so a stalled HNSW query fails fast instead of hanging the agent turn. Optionally add a small retry around the embed POST for transient 5xx/connection errors.

#### ⚪ LOW · No test scaffolding/stub for the clone-and-fill surfaces (custom Retriever / custom tool)

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:dev-acceleration / test scaffolding
- **證據**:The tests/ dir has ~60 harness tests but none is a copyable template for the surfaces a dev is told to fill. No test file matches template/stub/example/scaffold/skeleton (ls tests/ filter returned none). README.md:139-166 instructs devs to implement their own Retriever (option C) and custom tools, and README.md:222-227 tells them to run pytest, but offers no starting test for those custom pieces — the closest, tests/test_retriever.py, only exercises the built-in DummyRetriever. A dev wiring a real retriever has no skeleton to copy.
- **建議**:Ship a commented examples/tests/test_my_retriever.py (or tests/test_custom_retriever_template.py) that subclasses/implements Retriever, asserts the search/fetch contract from retrieval/base.py:30-32, and shows the set_retriever() wiring and a @anila_tool test. Reference it from the README 'fill the project' section so devs start from a passing test instead of a blank file.

#### ⚪ LOW · No dockerized hello-world agent example

- **可信度**:⚠️ 待複驗(finder confidence=medium)
- **類別**:dev-acceleration / containerization example
- **證據**:No Dockerfile/docker-compose exists in the template (find for Dockerfile*/docker-compose*/*.dockerfile under anila-agent excluding the vendored templete/ returned none). README.md:229-235 explains the template is mounted into the CSP platform's compose stack as a read-only download artifact, but the template itself provides no container recipe for a dev to run the agent standalone. The pyproject.toml:36-37 entry point (`anila`) is the only documented run path.
- **建議**:Add a Dockerfile (python:3.12-slim, pip install -e ., ENTRYPOINT ["anila"]) plus a docker-compose.yml that passes ANILA_BASE_URL/ANILA_API_KEY/ANILA_MODEL via env and mounts ANILA_HOME as a volume, documented under a new README 'Run in Docker' subsection. This gives devs a one-command containerized hello-world without standing up a venv, complementing the OpenAI-service-wrapper example above.

#### ℹ️ INFO · Retriever Protocol is clean and consistent (positive finding)

- **可信度**:⚠️ 待複驗(finder confidence=certain)
- **類別**:API design
- **證據**:anila_agent/retrieval/base.py:18-39 defines a minimal two-method Protocol (search/fetch) plus name/metadata properties, with a clear contract docstring (search returns <=k Documents desc by relevance, never None; fetch returns Document or None). Signatures are consistent across DummyRetriever (dummy.py:44-68), PgVectorRetriever (pgvector.py:64-87), and AnilaPgVectorRetriever (anila_pgvector.py:145-185): all async, all return the shared pydantic Document (models/schemas.py:39-45). Backend-specific concerns (reranking, RLS, halfvec) stay in implementations, not the protocol. The async-only decision is explicitly stated and uniformly applied.
- **建議**:No change needed. The one caveat (sync impls slip past the runtime_checkable guard) is covered in a separate finding.
