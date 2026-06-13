# myCSPPlatform (CSP — Control & Data Plane)

> ANILA's authoritative core service: owns users, API keys, model / agent registration, conversations and audit, and exposes an OpenAI-compatible proxy.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This service exists on every ANILA deployment branch. Each branch's target / auth / differences are in the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md). **Auth mode is branch-dependent**: `main` / `prod-public-passwd` / `prod-military-passwd` / `dev-*` / `trial-military` are **password-only**; `prod-intranet-card` additionally carries **SSO (OIDC) + MND PKI smart-card login** (`/api/auth/card/*`), `users.local_password_disabled` SSO-only, and `GET /api/auth/revocations` (anila-studio cold-start sync). These are that branch's permanent fork zone — never pushed back to main.

---

## Overview

`myCSPPlatform` (codename **CSP**) is ANILA's authoritative store: Router, ingestion-worker, anila-studio and the frontends all ask it for user identity, API keys, model / agent manifests and usage data. It plays two planes:

- **Control Plane** — `/api/*` (JWT / cookie auth): management & internal communication — users, API keys, model registry, agent registration & approval, conversations / attachments / shares / handoffs, audit, alerts, trusted-hosts, user memory, service tokens.
- **Data Plane** — `/v1/*` (`sk-` API key or cookie): OpenAI-compatible proxy. Any OpenAI SDK / curl can target CSP directly; it routes by `model_type` to backend LLM / Embedding / VLM / Agent and writes unified `token_usage` billing.

CSP also carries one application pipeline and fronts one extracted service:

- **Ingestion knowledge base** — upload → chunk → embedding → pgvector retrieval (RAG). CSP enqueues work to Redis via `arq`, consumed by the standalone [`ingestion-worker`](../ingestion-worker/) container.
- **Studio deck generation** — **extracted into the standalone [`anila-studio`](../anila-studio/) service on 2026-05-23 (PR #12)**. CSP only keeps the contract endpoints: `/api/ingestion/.../search`, `/images/search`, `/images/{id}/blob`, `/.well-known/jwks.json`, `/api/auth/revocations`, plus the Redis token-revoke publisher. See [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md).

> Platform-wide positioning: root [`../README.md`](../README.md) and the single source of truth [`../anila_plan.md`](../anila_plan.md).

---

## Architecture & Stack

```
                       ┌──────────────┐
        Users / SDK ─▶ │    Nginx     │ public entry (reverse proxy + static SPA + 6 security headers)
        / Router       └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │   FastAPI    │ csp :8000  (app.main:app)
                       │  /api/*  ──── Control Plane (JWT / cookie)
                       │  /v1/*   ──── Data Plane    (sk- API key)
                       └──┬────┬───┬──┘
              ┌───────────┘    │   └────────────┐
        ┌─────▼──────┐  ┌──────▼──────┐  ┌──────▼────────┐
        │  csp-db     │  │  Redis      │  │ model / agent  │
        │ (pgvector)  │  │ (arq queue) │  │  endpoints     │
        └─────────────┘  └──────┬──────┘  └────────────────┘
                                │ enqueue
                         ┌──────▼──────────┐
                         │ ingestion-worker │ (standalone container)
                         └──────────────────┘
```

### Backend `backend/` (FastAPI / Python)

| Item | Details |
|------|---------|
| Language / framework | Python 3.11 · FastAPI · Uvicorn[standard] |
| ORM / migration | SQLAlchemy 2.0 · Alembic |
| DB driver | psycopg2-binary (PostgreSQL 16 + pgvector) |
| Auth | python-jose (JWT, HS256) · passlib[bcrypt]; `prod-intranet-card` adds OIDC + PKCS#7/CMS card verification |
| HTTP client | httpx (proxies downstream models / agents) |
| Queue | arq (pushes ingestion work to Redis) |
| Text post-processing | opencc-python-reimplemented (Simplified→Traditional + Taiwan terms) |
| Image / diagram | FLUX backend (HTTP) · Graphviz `dot` (renders `Slide.diagram_dot`) |
| Tests | pytest · pytest-asyncio · respx (mock FLUX backend) |

Full deps in [`backend/requirements.txt`](./backend/requirements.txt). The backend image also installs `graphviz` and `fonts-noto-cjk` so DOT diagrams with CJK labels render (see [`backend/Dockerfile`](./backend/Dockerfile)).

### Frontend `frontend/` (Vue 3 / Vite)

| Item | Details |
|------|---------|
| Framework | Vue 3 (`^3.5`) + Vite 6 |
| State / routing | Pinia · vue-router |
| HTTP / charts | axios · ECharts + vue-echarts (usage time series) |
| Styling | Tailwind CSS + PostCSS |

The frontend is a pure SPA admin console (dashboard / API keys / models / users / usage / developer agents / trusted-hosts), served as static files by Nginx. Deps in [`frontend/package.json`](./frontend/package.json).

---

## Layout

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app entry (app.main:app)
│   │   ├── config.py · database.py
│   │   ├── api/                   # router.py + auth/users/api_keys/models/agents
│   │   │   ├── proxy.py           # /v1/* OpenAI-compatible proxy (Data Plane)
│   │   │   ├── conversations.py attachments.py public_share.py handoffs.py memory.py
│   │   │   ├── service_clients.py service_access_grants.py trusted_hosts.py
│   │   │   └── ingestion/         # knowledge base RAG: documents / jobs / search / ...
│   │   ├── models/ schemas/ services/   # ORM / Pydantic / auth·proxy·startup_security ...
│   │   ├── middleware/api_key_auth.py
│   │   └── utils/
│   ├── migrations/                # Alembic
│   ├── tests/                     # pytest
│   ├── requirements.txt
│   └── Dockerfile                 # python:3.11-slim + graphviz + noto-cjk
├── frontend/                      # Vue 3 SPA
├── docker/                        # standalone Dockerfile / docker-compose.yml / nginx.conf
├── scripts/ · start.sh · .env.example
└── README.md / README.en.md
```

---

## Setup & Run

### Integrated (recommended) — as the `csp` service of the ANILA stack

Start from the **repo root** via compose:

```bash
docker compose -f docker-compose-dev.yml up -d --build csp   # dev
# prod: docker compose up -d csp (prod branches can use scripts/deploy-prod.sh)
```

The stack also includes `csp-db` (pgvector/pg16), `redis`, `ingestion-worker`, `router`, `pptx-renderer`, `anilalm`, `anila-ui`, `nginx`. CSP joins two networks: `default` (in-stack) and `anila-models-net` (external, to reach `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`).

> On first start, if `anila-models-net` doesn't exist: `docker network create anila-models-net`.

### CSP standalone development

```bash
cd myCSPPlatform
cp .env.example .env       # at minimum change SECRET_KEY and ADMIN_PASSWORD
./start.sh up              # up / down / restart / logs [csp] / status / build / shell
```

Backend locally (no container, bring your own PostgreSQL):

```bash
cd myCSPPlatform/backend && source .venv/bin/activate
pytest
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Key environment variables

| Variable | dev default | Notes |
|------|----------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | App DB (the `csp_app` role is RLS-bound) |
| `MIGRATION_DATABASE_URL` | `postgresql://csp:csp@csp-db:5432/csp` | Higher-privilege role for migrations |
| `SECRET_KEY` | `dev-secret-key-change-in-prod` | JWT signing key, **must change in prod** (else `startup_security` refuses to boot) |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | JWT settings |
| `CSP_SERVICE_TOKEN` | `dev-service-token` | legacy fleet-shared s2s token (fallback); each agent now uses its own `csk-` |
| `REDIS_URL` | `redis://redis:6379` | arq ingestion queue |
| `INGESTION_UPLOAD_DIR` | `/var/anila/ingestion-uploads` | upload scratch dir |
| `AUTO_REGISTER_MODELS` / `AUTO_REGISTER_AGENTS` | see compose | declarative model / agent registration at startup |
| `AUTO_SEED_API_KEYS` | see compose | seed users + `sk-` keys at startup |
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | enables FLUX images; empty = off |
| `FLUX_MAX_CONCURRENT` / `FLUX_TIMEOUT_SECONDS` | `4` / `180` | FLUX concurrency / timeout |
| `ANILA_TRUSTED_HOSTS` | `gemma4,gpt-oss-20b,nv-embed-proxy,host.docker.internal` | SSRF guard allow-list bootstrap (then managed via `/trusted-hosts` UI) |

> When unset, `FLUX_CACHE_DIR` defaults to `$INGESTION_UPLOAD_DIR/flux-cache`; FLUX images are content-addressed by `SHA256(prompt + aspect_ratio)`. **`prod-intranet-card` also has `ENABLE_CARD_LOGIN` / `REQUIRE_CARD_LOGIN_ONLY` / `CARD_INITIAL_OWNERS`** (see that branch's root README).

---

## Integration

| Service | Relationship |
|------|------|
| **csp-db** (pgvector/pg16) | Main database; also backs Ingestion vector search |
| **redis** | arq queue (CSP `enqueue_ingest_document()` → `ingestion-worker`); also token-revoke pub/sub |
| **ingestion-worker** | Standalone container; chunk / embed / write pgvector |
| **gemma4 / gpt-oss-20b** (LLM) | Proxied via `anila-models-net` through `/v1/chat/completions`; gemma4 is Router primary |
| **nv-embed-proxy** (Embedding) | `/v1/embeddings` proxy target; Ingestion uses it too |
| **flux2-dev / flux2-dev-agent** (FLUX) | `image-generator` agent; Studio / chat drawing produce images here |
| **router** (anila-core-router) | Pulls `/v1/agents` manifest for dispatch; authenticates with a service token |
| **anila-studio** | HTTP-only calls to CSP's search / proxy / JWKS / revocations contract endpoints |
| **anilalm / anila-ui** | Frontends, via `/api/*` (management / chat) and `/v1/*` (inference) |

Proxy example (OpenAI-compatible):

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemma4","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

> **Encryption one-way latch**: if a request routes to an agent marked `requires_encryption`, the whole conversation latches to `classified=true` — ANILA's core "main LLM unencrypted → upgrade the whole conversation on hitting an encrypted agent" mechanism, with no downgrade path in the UI.

---

## Related docs

- Ingestion platform design: [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)
- Parent-child RAG: [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- Multi-service integration: [`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md)
- SSO migration: [`../docs/platform/sso-migration.md`](../docs/platform/sso-migration.md)
- Service-token cutover: [`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- Studio / FLUX spec: [`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- Platform: [`../README.md`](../README.md) · Roadmap: [`../anila_plan.md`](../anila_plan.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

---

**Role**: Control + Data Plane · **Authoritative for**: users · api_keys · models · agents · service_clients · token_usage · audit_logs · ingestion KB (Studio extracted to anila-studio; CSP keeps only the contract endpoints)
