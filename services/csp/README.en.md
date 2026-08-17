# services/csp (CSP — Control & Data Plane)

> ANILA's authoritative core service (formerly `myCSPPlatform`): owns users, API keys, model / agent registration, the Task spine, Full Trace, four-level classification governance, conversations, the knowledge base and audit — and fronts an OpenAI-compatible proxy.

> 繁體中文版：[`README.md`](./README.md)

> 🧭 **This file reflects the post-redesign reality** (`anila-redesign` branch): the four-way top layout `services/ apps/ packages/ infra/`, the root compose shim (`compose.yaml` → `infra/compose/platform.yml`), deploy scripts under `infra/deployment/{scripts,intranet}/`, and the Slice 2–9 capabilities relevant to CSP (Task spine, Full Trace, four-level classification, Agent Registry, Model Gateway, Service Registry, Artifact contract). Design authority lives in [`docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/): the constitution [`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md) and this service's domain doc [`03-csp-governance-control-plane.md`](../../docs/anila-redesign-docs/03-csp-governance-control-plane.md).

---

## 0. One-line role

CSP is ANILA's authoritative store and **dual-plane gateway**: the Router, ingestion-worker, anila-studio and every frontend ask it for identity, API keys, model / agent manifests and usage. It backs the product-facing **Governance Center** (`apps/csp-governance-ui`), **Task Center** (Task spine), **Artifact Center** (Artifact contract) and **Project Entry** (Service Registry / launch gateway).

- **Control Plane — `/api/*`** (RS256 JWT / cookie auth): governance and internal platform traffic. Users, API keys, model / agent registration + approval, tasks, policy decisions, four-level classification governance, conversations / attachments / shares / handoffs, audit, alerts, banners, departments, Service Registry, trusted-hosts, user memory, service tokens / service clients.
- **Data Plane — `/v1/*`, `/v2/*`** (`sk-` API key or cookie / service token): OpenAI-compatible proxy that routes by `model_type` to backend LLM / Embedding / VLM / Agent and writes `token_usage` for billing; it also ingests Full Trace spans (`POST /v1/traces/{trace_id}/spans`).

CSP also hosts the **Ingestion knowledge base** (document → chunk → embedding → pgvector RAG + cross-document relations, pushed via `arq` onto a Redis queue consumed by the standalone [`ingestion-worker`](../ingestion-worker/)) and integrates the extracted [`anila-studio`](../anila-studio/) (slides / reports / image generation); on the CSP side only the contract endpoints and the **durable Artifact job store** remain.

---

## 1. Architecture & stack

```
                       ┌──────────────┐
        User / SDK  ─▶ │    Nginx     │ edge (reverse proxy + static SPA + security headers)
        / Router       └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │   FastAPI    │ csp :8000  (app.main:app)
                       │  /api/*  ──── Control Plane (RS256 JWT / cookie)
                       │  /v1,/v2 ──── Data Plane (sk- / service token)
                       └──┬────┬───┬──┘
              ┌───────────┘    │   └────────────┐
        ┌─────▼──────┐  ┌──────▼───────┐  ┌──────▼────────┐
        │  postgres   │  │  Redis       │  │ model / agent  │
        │ (pgvector)  │  │ (arq+pub/sub)│  │  endpoints     │
        └─────────────┘  └──────┬───────┘  └────────────────┘
                                │ enqueue
                         ┌──────▼───────────┐
                         │ ingestion-worker  │ (separate container)
                         └───────────────────┘
```

| Item | Detail (from `requirements.txt` / `infra/docker/csp.Dockerfile`) |
|------|------|
| Language / framework | Python 3.11 · FastAPI 0.136.1 · uvicorn[standard] 0.34.0 |
| ORM / migration | SQLAlchemy 2.0.36 · Alembic 1.14.1 (legacy `0001`–`0046` [no 0025] then redesign `r1_0001`–`r1_0008`) |
| Config | pydantic-settings 2.7.1 (`app/config.py`) |
| Auth | **JWT is RS256** (asymmetric, `app/utils/security.py` + JWKS; `python-jose[cryptography] 3.5.0`) · bcrypt 5.0.0 (direct, cost 12) |
| DB drivers | psycopg2-binary 2.9.10 (PostgreSQL 16 + pgvector) + asyncpg (`csp_app` RLS pool, ingestion) |
| HTTP client | httpx 0.28.1 (proxies downstream models / agents) |
| Queue | arq 0.26.1 (ingestion / eval / relation-reresolve onto Redis) + Redis pub/sub (token revoke) |
| Text post-processing | opencc-python-reimplemented 0.1.7 |
| Tests | pytest · pytest-asyncio 0.24.0 · respx 0.22.0 |

> The deployed image uses [`infra/docker/csp.Dockerfile`](../../infra/docker/csp.Dockerfile) (multi-stage, bundles `anila-core[rag]`), and it is now the **only** one — a second `services/csp/Dockerfile` that compose never built was deleted on 2026-08-06 ([FAKE-CONTROLS](../../docs/FAKE-CONTROLS.md) #50). The governance frontend now lives at the top level, [`apps/csp-governance-ui/`](../../apps/csp-governance-ui/) (Vue 3 / Vite, "官方藍" visual redesign), served statically by Nginx.
>
> The container runs as **uid 10001 (non-root)**. `/app/logs` is the only writable path baked into the image; uploads, attachments, `share/pki` and `secrets/` all live on bind mounts whose ownership is decided by the host — so [`infra/deployment/scripts/fix-runtime-ownership.sh`](../../infra/deployment/scripts/fix-runtime-ownership.sh) must run before the stack comes up (deploy-prod.sh's `deploy`/`up`/`rebuild` paths and intranet-deploy.sh `[4c]` all call it).
>
> ⚠ Widening under `secrets/` is an **allow-list** (the JWT keypair and the dev-card-ca bundle, nothing else). A new file the runtime needs to read must get its own `widen_file` line in that script, otherwise it stays unreadable. The trade is deliberate: no future private key dropped into `secrets/` becomes group-readable by gid 10001 by accident.

---

## 2. Module boundaries (`app/modules/`)

The redesign carves the four MVP cores into **mutually independent** modules, enforced by an import-linter contract ([`.importlinter`](./.importlinter), run by `infra/ci/lint-boundaries.sh`): `tasks / policy / launch / artifacts` **must not import one another**, and `app.modules.*` **must not import `app.api`** (one-way `api → modules` layering).

| Module | Files | Responsibility |
|--------|-------|----------------|
| `app.modules.tasks` | `router.py` · `service.py` | Task / TaskRun lifecycle (ten-value state machine), the three SourceSnapshot rules, mandatory `trace_id` (doc 01 / doc 03). |
| `app.modules.policy` | `router.py` · `service.py` | Append-only PolicyDecision record (fail-closed; a deny must carry a reason), ceiling pure functions, and the four-level classification latch core (`apply_classification`, one-way; `無機密 < 營業秘密 < 密 < 機密`). |
| `app.modules.launch` | `manifest.py` · `service.py` · `token.py` | Launch Gateway primitives: `service_launches` rows, launch URLs, RS256 launch token (doc 07 §6). **Zero** policy/task/api coupling — access control is orchestrated by `app.api.services`. |
| `app.modules.artifacts` | `service.py` | Persistence of the four artifact tables, fail-closed binding, owner-scoped reads. The classification latch and PolicyDecision are done by the orchestrator (`app.api.artifacts`) calling policy. |

---

## 3. Auth surfaces (`app/api/auth/` package)

The auth router was split from a single file into a package, one submodule per auth form, all mounted under the `/api/auth` prefix (`_common.py`):

| Submodule | Routes (`/api/auth` prefix) | Notes |
|-----------|-----------------------------|-------|
| `password.py` | `POST /register` · `POST /login` · `POST /refresh` · `POST /logout` · `GET /me` · `PUT /password` | Password login → RS256 JWT (access + refresh) + cookie. |
| `oidc.py` | `GET /providers` · `GET /oidc/{provider_id}/start` · `GET /oidc/{provider_id}/callback` | Enterprise SSO / OIDC authorization-code flow (providers managed via `/api/auth-providers`). |
| `card.py` | `GET /card/challenge` · `POST /card/verify` | NCSIST CSPKI natural-person smart-card login: **real** PKCS#7 / CMS signature verification (SignerInfo signature + cert chain + nonce anti-replay, `app/services/card_auth.py`). |
| `registration_tokens.py` | one-time registration-token surface | Controlled self-service registration. |
| `revocations.py` | `GET /revocations` | Service-token-authenticated revocation cold-start sync (consumed by anila-studio). |

> All three login forms (password / oidc / card) **coexist in the redesign tree's code**; enablement is decided by the single `ANILA_AUTH_MODE` (`password` / `mixed` / `card-only`) and whether an SSO provider is registered. The admin CRUD for SSO / OIDC providers is the separate `app/api/auth_providers.py` (prefix `/api/auth-providers`).

---

## 4. API surface: Data Plane vs Control Plane

### Data Plane (`/v1/*`, `/v2/*`) — OpenAI-compatible proxy + Trace ingest

`app/api/proxy.py` (no APIRouter prefix — full paths, so nginx `/v1` passthrough reaches them):

- `GET /v1/agents` — Router fetches agent manifests.
- `GET /v1/models` — permission-filtered model list.
- `POST /v1/chat/completions` — agent-first then model; streaming + non-streaming; memory injection; classified one-way latch; **Task spine**: may carry `X-ANILA-Task-Id` — when present the call is validated against the Task spine, a `PolicyDecision(action="task.run")` is recorded, and a `TaskRun` brackets the proxied call; when absent the usage row is marked `legacy_runtime_call=true` (`app/services/proxy/task_link.py`).
- `POST /v1/agents/{agent_name}/sessions/{session_id}/answer` — Router resume passthrough.
- `POST /v1/embeddings`, `POST /v2/embeddings`.

Full Trace ingest (`app/api/traces.py`, also full-path):

- `POST /v1/traces/{trace_id}/spans` — data-plane span collection (`202`, batch 1..256, `(trace_id, span_id)` idempotent upsert-ignore, fail-safe / non-propagating). Auth = any data-plane credential. The producer is [`anila_trace_sdk`](../../packages/anila-core/src/anila_core/tracing/sdk.py) (inside `packages/anila-core`, fail-open, batching background exporter).
- `GET /api/traces/{trace_id}` — control-plane read (admin/owner or the requester of the task that owns the trace).

### Control Plane (`/api/*`)

- **New in the redesign**: `/api/tasks` (`tasks` module: create / list / get / `/{id}/runs`), `/api/policy-decisions`, `/api/classification/inventory` (classification stock-take), `/api/classification/declassification-requests` (declassification request + supervisor approval), `/api/classification-authorities` (classification approval authority), `/api/services` (Service Registry: CRUD + `/{id}/launch` + `/{id}/audit-callbacks` + `/{id}/manifest` + `/{id}/project-bindings`), `/api/artifacts` (+ data-plane `POST /v1/artifact-jobs` etc. as the Studio report surface).
- **Existing governance**: `/api/auth`, `/api/auth-providers`, `/api/keys`, `/api/models` (incl. `set-router-primary` / `activate` / `purge`), `/api/agents` (register / approve / reject / health-check / credentials / template), `/api/users`, `/api/departments`, `/api/usage`, `/api/alerts`, `/api/audit-logs`, `/api/banners`, `/api/memory`, `/api/platform-links`, `/api/service-clients`, `/api/service-access-grants`, `/api/trusted-hosts`, `/api/conversations` (incl. `/search`, shares, ratings), `/api/attachments`, `/api/handoffs` + `/api/notifications`, `/api/public/share/{token}` (unauthenticated, gated by `ENABLE_PUBLIC_SHARE`), `/api/ingestion/*`.
- **Other**: `GET /.well-known/jwks.json` (RFC 7517, unauthenticated, `max-age=3600`), `GET /health`, `GET /docs` + `/openapi.json` (admin tier only), SPA catch-all (with path-traversal guard).

Proxy example:

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemma4","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

---

## 5. New tables / migrations (`r1_0001`–`r1_0008`, one line each)

The redesign series follows the legacy numeric chain (`r1_0001` revises `0046`), staying linear; enums are always stored as open `String` (closed enums are enforced at the Pydantic contract layer `app/schemas/contracts/`), and JSON uses `with_variant(JSONB, "postgresql")` to stay portable.

| Revision | Slice | What it does |
|----------|-------|--------------|
| `r1_0001` | 2a | Task / Trace / Policy six-table foundation: `tasks` · `task_runs` · `source_snapshots` · `citations` · `policy_decisions` · `trace_spans`; `classification_level` defaults to `無機密`. |
| `r1_0002` | 2b-C | `token_usage` ↔ task link: `task_id` (FK `ON DELETE SET NULL` + partial index) and a `legacy_runtime_call` boolean flag (marks task-less `/v1` chat legacy traffic). |
| `r1_0003` | 3a | Four-level classification schema upgrade (`無機密` / `營業秘密` / `密` / `機密`) + three governance tables: `classification_events` · `declassification_requests` · `classification_authority_assignments`; adds the four common classification columns to existing resources (conversations / messages / collections / documents …) and backfills (`classified=true → 機密` floor; `requires_encryption=true → 密`). |
| `r1_0004` | 5a | Agent Registry upgrade: `agents.approval_status` grows from three values into a **seven-state machine** (`draft` / `pending_connection_test` / `pending_trace_test` / `pending_security_review` / `approved` / `rejected` / `disabled`), plus manifest / trace-test / runtime columns. |
| `r1_0005` | 6a | Model Gateway hardening: `model_registry` formalized into `ModelEndpoint` (`protocol` / per-model `api_key_secret_ref` AES-GCM envelope / `classification_ceiling` / `supports_*`); `health_status` collapses to **five states** (`healthy` / `degraded` / `unhealthy` / `unknown` / `disabled`). |
| `r1_0006` | 7a | Service Registry: `platform_links` additively upgraded into `registered_services` (33 fields, id preserved) + `service_launches` · `service_audit_callbacks` · `service_project_bindings`; `service_access_grants` gains a `service_id` FK. |
| `r1_0007` | 8a | Artifact contract, four tables: `artifacts` · `artifact_versions` · `export_records` · `artifact_jobs` (**durable** state for Studio's five job pipelines → satisfies "restart never loses a job"; Studio reports over HTTP with its service token, never reading the CSP DB directly). |
| `r1_0008` | R-SEC | `registered_services.service_client_id` FK: binds audit-callbacks to the Service Client that *belongs to* the target service; fail-closed / default-deny, an unbound service rejects all callbacks (`403`). |

---

## 6. Security invariants

- **Four-level one-way classification latch**: order `無機密 < 營業秘密 < 密 < 機密`; effective level = `max` of observed classifications and **never downgrades** (`policy.apply_classification` writes a `ClassificationEvent`). **Declassification is not a removed route but a governed request workflow**: `declassification_requests` + supervisor approval (`classification_authority_assignments`), fail-closed default `pending_supervisor`.
- **Card SSO**: the CSPKI natural-person smart card uses real PKCS#7 / CMS verification (SignerInfo signature + cert chain + nonce anti-replay), not mere parsing.
- **JWT / JWKS**: RS256 (access + refresh, `tv` token-version revocation claim); `GET /.well-known/jwks.json` publishes the verification keys. The launch token reuses the same RS256 keypair / `kid`, so registered services verify it **locally** via JWKS (`aud` / `iss` / `exp` / signature); TTL 10 min, and it **never** embeds a model key or a long-lived user JWT.
- **CSRF**: cookie-authenticated mutating requests use double-submit (`X-CSRF-Token`, constant-time compare, `CsrfMiddleware`).
- **RLS / `csp_app`**: the runtime uses the non-privileged `csp_app` role (so RLS actually fires); only migrations use the escalated `csp` superuser (see §8).
- **SSRF url_guard kind split (Slice 6a, doc 04 §8)**: `anila_core.security.validate_outbound_url(url, endpoint_kind=...)` domain-splits the http flag across `model` / `agent` / `generic` — a model endpoint rejects http by default and **admits it only via an explicit `ANILA_ALLOW_HTTP_ENDPOINT=1` (PLAN.md P0.2, 2026-07-29: uniform across production and dev; the intranet model gateway speaks plain http)**; an agent endpoint is allowed over http via `ANILA_ALLOW_HTTP_AGENT_ENDPOINT` (legacy `ANILA_ALLOW_HTTP_ENDPOINT` still works as a deprecation-warned fallback, for the intranet MLSteam plain-http NodePort agent). The allow-list = the `trusted_hosts` table + the `ANILA_TRUSTED_HOSTS` env.
- **Credential encryption**: AES-256-GCM (`anila-core` `credential_crypto` / `service_token_envelope`; covers per-model `api_key_secret_ref`, `csk-` agent credentials, ingestion credentials).
- **Token revocation**: durable `token_revocations` table + JWT `tv` enforcement + Redis fan-out; `/api/auth/revocations` for cold-start sync.
- **startup_security**: in prod, dev defaults for `SECRET_KEY` / `ADMIN_PASSWORD` / `CSP_SERVICE_TOKEN` / DB passwords refuse to boot (an empty `SECRET_KEY` is always fatal; `ANILA_ALLOW_DEV_SECRET=1` downgrades to a warning). Inbound hardening also includes a CORS allow-list (no `*` fallback), optional TrustedHostMiddleware, SPA path-traversal guard, and nginx security headers + rate-limit.

---

## 7. Testing

Tests are sqlite-backed (`tests/conftest.py` points `DATABASE_URL` at a per-session temp file, deleted when the session ends, so they never touch Postgres / running containers) and need no environment variables exported first:

```bash
python -m pytest services/csp/tests -q   # from the repo root
cd services/csp && python -m pytest -q   # or from here; both MUST agree
```

**Current baseline (measured 2026-07-31)**: **1 failed · 1296 passed · 13 skipped · 0 errors** (~8 min). The single red is `test_template_download.py::test_developer_can_download_template` — a **real production defect** (template directory resolution returns 404), not a test problem.

📌 Full write-up in **[`tests/README.md`](tests/README.md)** — why the old "26 failing" baseline was fiction (the same code gave 27 vs 14 depending on which directory you ran from), the execution-order pollution and its fix, and the two-layer structure of the card-login tests.

---

## 8. Alembic notes

- **`r1_` namespace**: redesign migrations use the `r1_` prefix and chain linearly after the legacy numeric series (`r1_0001` has `Revises: 0046`). When adding a module / table, update the `.importlinter` contract and `app/schemas/contracts/` in lockstep.
- **`MIGRATION_DATABASE_URL` (escalated, alembic-only)**: migrations need a superuser-class connection (`0014` runs `CREATE EXTENSION` / `CREATE ROLE csp_app`). The runtime `DATABASE_URL` points at the non-privileged `csp_app` (so RLS fires); `MIGRATION_DATABASE_URL` is alembic's escalated stand-in, falling back to `DATABASE_URL` when unset (`migrations/env.py`). Compose splits the two: runtime `csp_app:...`, migration `csp:...`.
- **Auto-upgrade on boot**: the `app/main.py` lifespan runs `alembic upgrade head` programmatically via `command.upgrade(cfg, "head")` (falling back to `create_all` only on failure).

---

## 9. Running & deployment

The full stack (redis / ingestion-worker / router / anila-studio / frontends / nginx) is defined by the root compose shim: `compose.yaml` → [`infra/compose/platform.yml`](../../infra/compose/platform.yml) (prod, project `anila-platform`) and `compose.dev.yaml` → `infra/compose/dev.yml` (dev).

```bash
# from repo root
docker compose -f compose.dev.yaml up -d --build csp    # dev
docker compose up -d csp                                 # prod (platform.yml)
# day-to-day lifecycle: infra/deployment/scripts/deploy-prod.sh
# intranet card-login bootstrap: infra/deployment/intranet/intranet-deploy.sh
```

CSP joins two networks: `default` (in-stack) and `anila-models-net` (external, reaching `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`). On first boot if it doesn't exist: `docker network create anila-models-net`.

Local backend (no container, bring your own PostgreSQL): `cd services/csp && .venv/bin/python -m uvicorn app.main:app --port 8000`. Key env vars (`app/config.py` / compose): `DATABASE_URL` (runtime `csp_app`), `MIGRATION_DATABASE_URL` (escalated), `SECRET_KEY`, `JWT_KID`, `ADMIN_PASSWORD`, `ANILA_AUTH_MODE`, `CSP_SERVICE_TOKEN`, `MODEL_GATEWAY_API_KEY`, `ANILA_ENV` (deployment posture; since PLAN.md P0.2 it no longer affects the model-http gate), `ANILA_ALLOW_HTTP_ENDPOINT` / `ANILA_ALLOW_HTTP_AGENT_ENDPOINT` / `ANILA_ALLOW_PRIVATE_ENDPOINT`, `ANILA_TRUSTED_HOSTS`, `REDIS_URL`, `ENABLE_PUBLIC_SHARE`. JWT PEM paths are fixed at `secrets/jwt-{private,public}.pem`. See [`.env.example`](./.env.example).

---

## 10. Related docs

- Design authority: [`docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) — constitution [`00`](../../docs/anila-redesign-docs/00-product-constitution.md), CSP governance control plane [`03`](../../docs/anila-redesign-docs/03-csp-governance-control-plane.md), Model Gateway [`04`](../../docs/anila-redesign-docs/04-model-gateway-design.md), Agent Registry [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md), Service Platform [`07`](../../docs/anila-redesign-docs/07-registered-gui-service-platform.md), classified latch & policy engine [`08`](../../docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md), API / event contracts [`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md), migration & development guardrails [`10`](../../docs/anila-redesign-docs/10-migration-and-development-guardrails.md).
- Platform overview: [`../../README.md`](../../README.md).
- Module boundary contract: [`.importlinter`](./.importlinter) (`infra/ci/lint-boundaries.sh`).

---

**Role**: Control + Data Plane · **Authoritative for**: users · api_keys · models · agents · service_clients · **tasks · trace_spans · policy_decisions · classification** · registered_services · artifacts · token_usage · audit_logs · the ingestion knowledge base (Studio + FLUX + graph rendering are extracted into anila-studio; CSP keeps the contract endpoints and the durable artifact job store)
