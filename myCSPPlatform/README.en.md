# myCSPPlatform (CSP — Cloud Service Platform)

> The Control Plane and Data Plane of the ANILA platform: the authoritative store for users, API keys, model / agent registration, plus an OpenAI-compatible proxy.

> 繁體中文版本：[`README.md`](./README.md)

---

## Overview

`myCSPPlatform` (codename **CSP** in the code) is the core service of the ANILA platform. It plays two roles at once:

- **Control Plane** — `/api/*` (JWT / cookie auth): the management surface and internal platform plumbing. Owns users, API keys, model registry, agent registration & approval, conversations / attachments / shares / handoffs, audit, alerts, etc.
- **Data Plane** — `/v1/*` (`sk-` API key): an OpenAI-compatible proxy that lets any OpenAI SDK / curl call CSP directly as an endpoint, routing by `model_type` to backend LLM / Embedding / VLM / Agent services.

Beyond platform governance, CSP also hosts two application pipelines:

- **Ingestion knowledge base** — document upload → chunking → embedding → pgvector retrieval (RAG). CSP enqueues ingest jobs into Redis via `arq`; a separate `ingestion-worker` container consumes them.

> Within the wider ANILA system, CSP is the authoritative store: the Router, Worker and UI all ask it for user identity, API keys, the model / agent manifest, and usage data. For the platform as a whole see the repo root [`../README.md`](../README.md).

---

## Architecture & Stack

```
                       ┌──────────────┐
        Users / SDK ──▶│    Nginx     │ dev entrypoint (reverse proxy + static SPA)
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
                         │ ingestion-worker │ (separate container)
                         └──────────────────┘
```

### Backend `backend/` (FastAPI / Python)

| Item | Detail |
|------|--------|
| Language / framework | Python 3.11 · FastAPI · Uvicorn[standard] |
| ORM / migration | SQLAlchemy 2.0 · Alembic |
| DB driver | psycopg2-binary (PostgreSQL 16 + pgvector) |
| Auth | python-jose (JWT, HS256) · passlib[bcrypt] |
| HTTP client | httpx (proxies downstream models / agents) |
| Queue | arq (enqueues ingestion jobs into Redis) |
| Testing | pytest · pytest-asyncio · respx |

Full dependency list in [`backend/requirements.txt`](./backend/requirements.txt). The backend container additionally installs `graphviz` and `fonts-noto-cjk` so DOT graphs with Traditional Chinese labels render correctly (see [`backend/Dockerfile`](./backend/Dockerfile)).

### Frontend `frontend/` (Vue 3 / Vite)

| Item | Detail |
|------|--------|
| Framework | Vue 3 (`^3.5`) + Vite 6 |
| State management | Pinia |
| Routing | vue-router |
| HTTP | axios |
| Charts | ECharts + vue-echarts (usage time-series) |
| Styling | Tailwind CSS + PostCSS |

Dependencies in [`frontend/package.json`](./frontend/package.json). The frontend is a pure SPA admin console (dashboard / API keys / models / users / usage / Developer agents, etc.) served as static files by Nginx.

---

## Layout

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI app entrypoint (app.main:app)
│   │   ├── config.py              # pydantic-settings configuration
│   │   ├── database.py            # SQLAlchemy engine / session
│   │   ├── api/
│   │   │   ├── router.py          # aggregates all /api/* sub-routers
│   │   │   ├── auth.py users.py api_keys.py models.py agents.py
│   │   │   ├── proxy.py           # /v1/* OpenAI-compatible proxy (Data Plane)
│   │   │   ├── conversations.py attachments.py public_share.py handoffs.py
│   │   │   ├── memory.py          # /api/memory user memory
│   │   │   ├── service_clients.py service_access_grants.py trusted_hosts.py
│   │   │   └── ingestion/         # knowledge-base RAG: documents/jobs/search/...
│   │   ├── models/                # SQLAlchemy ORM (users, api_key, model_registry,
│   │   │                          #   agent, ingestion, token_usage, audit_log, ...)
│   │   ├── schemas/               # Pydantic schemas
│   │   ├── services/              # auth/proxy/health_checker/usage_writer/auto_seed
│   │   │                          #   ingestion_queue, ...
│   │   ├── middleware/api_key_auth.py
│   │   └── utils/
│   ├── migrations/                # Alembic versions
│   ├── tests/                     # pytest (incl. agent credential / cookie auth)
│   ├── requirements.txt
│   └── Dockerfile                 # python:3.11-slim + graphviz + noto-cjk
├── frontend/
│   ├── src/                       # views / components / stores / api / router
│   ├── package.json
│   ├── vite.config.js · tailwind.config.js
│   └── index.html
├── docker/
│   ├── Dockerfile                 # standalone-dev multi-stage (Node + Python)
│   ├── docker-compose.yml         # CSP-only (use repo-root compose for the full stack)
│   └── nginx.conf
├── scripts/
├── start.sh                       # standalone-dev launch script
├── .env.example
└── README.md / README.en.md
```

---

## Setup & Run

### Integrated run (recommended) — as the `csp` service of the ANILA dev stack

In the dev stack CSP is the `csp` service, started **from the repo root** via `docker-compose-dev.yml`:

```bash
# From the repo root (<project_root>)
docker compose -f docker-compose-dev.yml up -d --build csp
```

The dev stack also includes `csp-db` (pgvector/pg16), `redis`, `ingestion-worker`, `router`, `anila-ui`, `nginx`, etc. CSP joins two networks: `default` (intra-stack) and `anila-models-net` (external, used to reach `gemma4` / `gpt-oss-20b` / `nv-embed-proxy`).

> On first launch if `anila-models-net` does not exist: `docker network create anila-models-net`.

### Backend local development (no container)

The backend venv lives at [`backend/.venv`](./backend/.venv):

```bash
cd myCSPPlatform/backend
source .venv/bin/activate
# Run tests
pytest
# Run locally (bring your own PostgreSQL / set DATABASE_URL)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### CSP standalone development (without the full stack)

```bash
cd myCSPPlatform
cp .env.example .env   # at minimum change SECRET_KEY and ADMIN_PASSWORD
./start.sh up          # up / down / restart / logs [csp] / status / build / shell
```

### Key environment variables (from the dev compose / Dockerfile)

| Variable | Dev default | Description |
|----------|-------------|-------------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | Application DB connection |
| `MIGRATION_DATABASE_URL` | `postgresql://csp:csp@csp-db:5432/csp` | Higher-privilege account for migrations |
| `SECRET_KEY` | `dev-secret-key-change-in-prod` | JWT signing key — **must change in production** |
| `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | JWT settings |
| `CSP_SERVICE_TOKEN` | `dev-service-token` | Legacy fleet-shared s2s token (fallback); each agent now uses its own `csk-` (issued by the register wizard, verified by CSP) — see `docs/guides/developer-guide.md` |
| `REDIS_URL` | `redis://redis:6379` | arq ingestion queue |
| `INGESTION_UPLOAD_DIR` | `/var/anila/ingestion-uploads` | Upload staging directory |
| `AUTO_REGISTER_MODELS` / `AUTO_REGISTER_AGENTS` | see compose | Declarative model / agent registration at startup |
| `AUTO_SEED_API_KEYS` | see compose | Seed users + `sk-` keys created at startup |
| `ANILA_TRUSTED_HOSTS` | `gemma4,gpt-oss-20b,nv-embed-proxy,host.docker.internal` | Allowed downstream proxy hosts |


---

## Integration

| Service | Relationship |
|---------|--------------|
| **csp-db** (pgvector/pg16) | CSP's primary database, also backs Ingestion's vector retrieval |
| **redis** | arq queue: CSP `enqueue_ingest_document()` enqueues, `ingestion-worker` consumes |
| **ingestion-worker** | Separate container that processes ingest jobs (chunking / embedding / writing pgvector) |
| **gemma4 / gpt-oss-20b** (LLM) | Proxied via `anila-models-net` through `/v1/chat/completions`; gemma4 is the Router primary |
| **nv-embed-proxy** (Embedding) | Target of `/v1/embeddings`, `/v2/embeddings`; also used by Ingestion for embeddings |
| **router** (anila-core-router) | Pulls the `/v1/agents` manifest from CSP for dispatch; authenticates with a service token |
| **anila-ui** | Frontend app that talks to CSP via `/api/*` (management / chat) and `/v1/*` (inference) |

OpenAI-compatible proxy example:

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma4", "messages": [{"role":"user","content":"Hello!"}], "stream": true}'
```

> One-way encryption latch: if a request is routed to an agent flagged `requires_encryption`, the whole conversation is latched to `classified=true`. This is ANILA's core security mechanism ("plaintext main LLM → escalate the whole conversation upon hitting an encrypted agent").

---

## Related docs

All paths below are verified to exist (docs were reorganized into topic folders):

- Ingestion platform design: [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md)
- Parent-child RAG design: [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- Service-token cutover runbook: [`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- Platform overview: [`../README.md`](../README.md)

---

**Role**: Control + Data Plane · **Authoritative for**: users · api_keys · models · agents · service_clients · token_usage · audit_logs · ingestion knowledge base
