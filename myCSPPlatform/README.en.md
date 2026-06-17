# myCSPPlatform (CSP — Control & Data Plane)

> ANILA's authoritative core service: owns users, API keys, model / agent registration, conversations, the knowledge base and audit, and exposes an OpenAI-compatible proxy.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This service exists on every ANILA deployment branch. Each branch's target / auth / differences are in the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md). **Auth mode is branch-dependent**: `main` and most branches are **password-only** (RS256 JWT + cookie; `/api/auth/*` has only register/login/refresh/logout/me/password/revoke/revocations); only **`prod-intranet-card`** adds SSO (OIDC) + MND PKI smart-card login (`/api/auth/card/*`) as a fork (the SSO / `local_password_disabled` columns were dropped from main in migration `0035`).

---

## Overview

`myCSPPlatform` (codename **CSP**) is ANILA's authoritative store: Router, ingestion-worker, anila-studio and the frontends all ask it for user identity, API keys, model / agent manifests and usage. It runs two planes:

- **Control Plane** — `/api/*` (RS256 JWT / cookie auth): management & internal comms — users, API keys, model registry, agent registration & approval, conversations / attachments / shares / handoffs, audit, alerts, banners, departments, platform links, trusted-hosts, user memory, service tokens / service clients.
- **Data Plane** — `/v1/*` and `/v2/*` (`sk-` API key or cookie): OpenAI-compatible proxy routing by `model_type` to backend LLM / Embedding / VLM / Agent, writing unified `token_usage` billing.

CSP also carries one application pipeline and fronts one extracted service:

- **Ingestion knowledge base** — upload → chunk → embedding → pgvector retrieval (RAG) + cross-document relations. CSP enqueues to Redis via `arq`, consumed by the standalone [`ingestion-worker`](../ingestion-worker/).
- **Studio deck / report generation** — **extracted into the standalone [`anila-studio`](../anila-studio/) service** (incl. FLUX image generation, Graphviz diagram rendering, PPTX/report pipelines). CSP keeps **only the contract endpoints**: ingestion `/search`, `/images/search`, `/images/{id}/blob`, the `/api/proxy` LLM path, `/.well-known/jwks.json`, `/api/auth/revocations`, plus the Redis token-revoke publisher. See [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md).

> Platform-wide positioning: root [`../README.md`](../README.md) and the single source of truth [`../anila_plan.md`](../anila_plan.md).

---

## Architecture & Stack

```
                       ┌──────────────┐
        Users / SDK ─▶ │    Nginx     │ public entry (reverse proxy + static SPA + security headers)
        / Router       └──────┬───────┘
                              │
                       ┌──────▼───────┐
                       │   FastAPI    │ csp :8000  (app.main:app)
                       │  /api/*  ──── Control Plane (RS256 JWT / cookie)
                       │  /v1,/v2 ──── Data Plane    (sk- API key)
                       └──┬────┬───┬──┘
              ┌───────────┘    │   └────────────┐
        ┌─────▼──────┐  ┌──────▼──────┐  ┌──────▼────────┐
        │  postgres   │  │  Redis      │  │ model / agent  │
        │ (pgvector)  │  │ (arq+pubsub)│  │  endpoints     │
        └─────────────┘  └──────┬──────┘  └────────────────┘
                                │ enqueue
                         ┌──────▼──────────┐
                         │ ingestion-worker │ (standalone container)
                         └──────────────────┘
```

### Backend `backend/` (FastAPI / Python)

| Item | Details (from `requirements.txt` / `docker/Dockerfile`) |
|------|---------|
| Language / framework | Python 3.11 · FastAPI 0.115.6 · uvicorn[standard] 0.34.0 |
| ORM / migration | SQLAlchemy 2.0.36 · Alembic 1.14.1 (migrations `0001`–`0045`) |
| Settings | pydantic-settings 2.7.1 |
| Auth | **JWT is RS256** (asymmetric, `app/utils/security.py` + JWKS; `python-jose[cryptography] 3.3.0`) · passlib[bcrypt] 1.7.4 + bcrypt 4.0.1 (pinned). **LDAP removed** |
| DB driver | psycopg2-binary 2.9.10 (PostgreSQL 16 + pgvector) + asyncpg (`csp_app` RLS pool for ingestion) |
| HTTP client | httpx 0.28.1 (proxies downstream models / agents) |
| Queue | arq 0.26.1 (ingestion / eval / relation-reresolve to Redis) + Redis pub/sub (token revoke) |
| Text post-processing | opencc-python-reimplemented 0.1.7 |
| Tests | pytest · pytest-asyncio 0.24.0 · respx 0.22.0 (~32 test files) |

> The container uses `docker/Dockerfile` (multi-stage, incl. `anila-core[rag]`); system packages `gcc` / `libpq-dev` / `curl` / `graphviz` / `fonts-noto-cjk`. `backend/Dockerfile` is dead (compose uses `docker/Dockerfile`). **JWT signing is RS256**: `ALGORITHM=HS256` is a legacy setting, unused for access/refresh.

### Frontend `frontend/` (Vue 3 / Vite, package `csp-platform`)

| Item | Details |
|------|---------|
| Framework | Vue 3.5.13 + Vite 6.0.5 |
| State / routing | Pinia 2.3.0 · vue-router 4.5.0 |
| HTTP / charts | axios 1.7.9 · ECharts 5.5.1 + vue-echarts 7.0.3 · **cytoscape 3.34.0** (`RelationGraph.vue`) |
| Styling | Tailwind 3.4.17 + PostCSS 8.4.49 |

A pure SPA admin console (21 views: dashboard / API keys / models / users / usage / developer agents / trusted-hosts / relation graph), served as static files by Nginx.

---

## Layout

```
myCSPPlatform/
├── backend/
│   ├── app/
│   │   ├── main.py            # lifespan: startup_security → alembic upgrade → startup_migrations
│   │   │                      #   → auto_seed → trusted_host backfill → health_checker / usage_writer
│   │   │                      #   / ingestion_pool; CORS / TrustedHost / CSRF / SPA fallback
│   │   ├── config.py · database.py
│   │   ├── api/               # router.py aggregates; per-resource routers (see API surface)
│   │   │   └── ingestion/     # collections / credentials / documents / eval_runs /
│   │   │                      #   image_blob / jobs / preview / relations / search
│   │   ├── models/            # 23 ORM files (user / agent / model_registry / ingestion /
│   │   │                      #   token_usage / audit_log / banner / department / ...)
│   │   ├── schemas/
│   │   ├── services/          # 26 services (auth / proxy / health_checker / usage_writer /
│   │   │                      #   auto_seed / startup_security / ingestion_queue /
│   │   │                      #   trusted_host / token_revocation_publisher / agent_credential ...)
│   │   ├── middleware/        # api_key_auth · caller · cookies · csrf
│   │   └── utils/             # security.py (RS256 JWT + JWKS keys) · time_helpers.py
│   ├── migrations/versions/   # Alembic 0001..0045
│   ├── tests/                 # ~32 pytest files
│   ├── scripts/generate-jwt-keypair.py
│   ├── requirements.txt
│   └── Dockerfile             # dead (compose uses docker/Dockerfile)
├── frontend/                  # Vue 3 SPA (src/views ×21 · components/cli design system)
├── docker/                    # Dockerfile (the real one) · docker-compose.yml (standalone csp+nginx+postgres) · nginx.conf
├── scripts/init_db.py · start.sh · .env.example
└── README.md / README.en.md
```

---

## Setup & Run

### Integrated (recommended) — as the `csp` service of the ANILA stack

The full stack (redis / ingestion-worker / router / anila-studio / frontends / nginx) is defined in the **repo-root** compose:

```bash
docker compose -f docker-compose-dev.yml up -d --build csp   # dev
# prod: docker compose up -d csp (prod branches can use scripts/deploy-prod.sh)
```

CSP joins two networks: `default` (in-stack) and `anila-models-net` (external, to reach `gemma4` / `gpt-oss-20b` / `nv-embed-proxy` / `flux2-dev`). On first start: `docker network create anila-models-net`.

> `myCSPPlatform/docker/docker-compose.yml` is a minimal **standalone-dev** compose (only `nginx` + `postgres` + `csp`, no redis / router); use the repo-root compose for full functionality.

### CSP standalone development

```bash
cd myCSPPlatform
cp .env.example .env       # at minimum change SECRET_KEY and ADMIN_PASSWORD
./start.sh up              # up / down / restart / logs [csp] / status / build / shell
```

Backend locally (no container, bring your own PostgreSQL): `cd backend && uvicorn app.main:app --port 8000`.

### Key environment variables (from `config.py`, actual defaults)

| Variable | Default | Notes |
|------|----------|------|
| `APP_NAME` / `APP_VERSION` | `CSP Platform` / `1.0.0` | identity; `/health` reports version |
| `DEBUG` | `False` | debug flag |
| `ENABLE_API_DOCS` | `False` | mounts `/docs` + `/openapi.json` only when true |
| `ENABLE_PUBLIC_SHARE` | `True` | enables unauthenticated `/api/public/share/{token}` |
| `DATABASE_URL` | `postgresql://csp:csp_password@localhost:5432/csp` (compose sets `@postgres:5432`) | DB connection |
| `SECRET_KEY` | `your-secret-key-change-this-in-production` | **no longer signs access/refresh JWTs** (RS256 now); used by startup_security + credential_crypto |
| `ALGORITHM` | `HS256` | **legacy / unused** (access/refresh use RS256) |
| `JWT_PRIVATE_KEY_PATH` / `JWT_PUBLIC_KEY_PATH` / `JWT_KID` | `secrets/jwt-private.pem` / `secrets/jwt-public.pem` / `anila-v1` | RS256 keys + JWKS kid |
| `ALLOW_AUTO_KEYGEN` | `False` | auto-generate keys if missing (**dev/test only**) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | `60` / `30` | JWT lifetimes |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `changeme` | seed admin; must override in prod |
| `CSP_SERVICE_TOKEN` | `""` | legacy fleet-shared s2s token (fallback) |
| `MODEL_GATEWAY_API_KEY` | `""` | Bearer injected for outbound model gateway (model calls only, not agent dispatch) |
| `EMBEDDING_TIMEOUT` / `LLM_TIMEOUT` | `30` / `120` | proxy timeouts (s) |
| `PROXY_MAX_RETRIES` / `PROXY_RETRY_BASE_DELAY` | `3` / `0.5` | proxy retries |
| `ALLOWED_ORIGINS` / `ALLOWED_HOSTS` / `COOKIE_SECURE` | see config | CORS allowlist / Host allowlist (`*`=off) / cookie secure |
| `AUTO_REGISTER_MODELS` / `AUTO_REGISTER_AGENTS` / `AUTO_REGISTER_LINKS` / `AUTO_SEED_API_KEYS` | `""` | declarative seed at startup |
| `ATTACHMENT_STORAGE_PATH` | `data/attachments` | attachment storage |

Read directly via `os.environ` (not in config.py): `ANILA_ALLOW_DEV_SECRET`, `INTERNAL_PLATFORM_API_KEY`, `ANILA_TRUSTED_HOSTS`, `REDIS_URL` (`ingestion_queue` default `redis://redis:6379`, `token_revocation_publisher` default `redis://redis:6379/0`), `INGESTION_UPLOAD_DIR` (`/var/anila/ingestion-uploads`).

> **`prod-intranet-card` also has** `ENABLE_CARD_LOGIN` / `REQUIRE_CARD_LOGIN_ONLY` / `CARD_INITIAL_OWNERS` (see that branch's root README); these do not exist on `main`.

---

## API surface

**Control Plane (`/api/*`)**: `/api/auth` (register / login / refresh / logout / me / password / revoke / **revocations** — **no card/SSO/OIDC on main**), `/api/keys`, `/api/models` (incl. `set-router-primary` / `activate` / `purge`), `/api/agents` (register / approve / reject / encryption / runtime-config / health-check / credentials / template/download), `/api/users`, `/api/departments`, `/api/usage`, `/api/alerts`, `/api/audit-logs`, `/api/banners`, `/api/memory`, `/api/platform-links`, `/api/service-clients`, `/api/service-access-grants`, `/api/trusted-hosts`, `/api/conversations` (incl. `/search`, shares, ratings), `/api/attachments`, `/api/handoffs` + `/api/notifications`, `/api/public/share/{token}` (unauthenticated, gated by `ENABLE_PUBLIC_SHARE`), `/api/ingestion/*`.

**Data Plane (`/v1/*`, `/v2/*`, `app/api/proxy.py`)**: `GET /v1/agents` (Router agent manifest), `GET /v1/models` (permission-filtered), `POST /v1/chat/completions` (agent-first then model, streaming + non-streaming, memory injection, classified latch), `POST /v1/agents/{name}/sessions/{sid}/answer` (Router resume passthrough), `POST /v1/embeddings`, `POST /v2/embeddings`.

**Other**: `GET /.well-known/jwks.json` (RFC 7517, unauthenticated, `max-age=3600`), `GET /health`, `GET /docs`+`/openapi.json` (only when `ENABLE_API_DOCS=true`), SPA catch-all (with path-traversal guard).

```bash
curl http://localhost/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemma4","messages":[{"role":"user","content":"Hello!"}],"stream":true}'
```

---

## Integration / auth

- **Called by**: Router (pulls `/v1/agents`, dispatches with a service token); anila-studio (contract endpoints — search / image-blob / JWKS / `/api/auth/revocations`, verifying CSP JWTs via JWKS); ingestion-worker (shared DB / queue); frontends via `/api/*` + `/v1/*`.
- **Calls out to**: registered models / approved agent endpoints (httpx + call-time SSRF guard + per-agent service-token injection); a model gateway (Bearer `MODEL_GATEWAY_API_KEY`); Redis (arq + pub/sub); Postgres + pgvector. Imports `anila_core` for SSRF guard / credential crypto / memory adapter / relation resolution / parsers / pg pool.
- **Auth mechanisms**: user RS256 JWT (access + refresh, `tv` token-version revocation claim) via Bearer or `anila_access_token` cookie; user API keys `sk-`; cookie session (`anila_access_token` / `anila_refresh_token` / `anila_csrf`) + double-submit CSRF (`X-CSRF-Token`, constant-time); s2s tokens `bsk-` (single-use bootstrap) / `csk-` (rotated agent) / service_clients (AES-256-GCM envelope + sha256 lookup hash + `hmac.compare_digest`) + legacy `CSP_SERVICE_TOKEN` fallback.

---

## Security highlights

- **Classified one-way latch**: agent `requires_encryption` → conversation `classified=TRUE` (`proxy.py`); memory referencing an encrypted source latches per Bell-LaPadula "no write down" (`ConversationMemoryChunk.is_encrypted`); upgrades only, the declassify route was removed (Phase K), classified conversations can't be shared.
- **SSRF guard**: `anila_core.security.validate_outbound_url` enforced at call time (proxy 502 / health_checker offline / before attaching the gateway key); allow-list from the `trusted_hosts` table + `ANILA_TRUSTED_HOSTS` env (30s TTL cache).
- **startup_security**: `assert_no_dev_defaults()` refuses to boot in prod when `SECRET_KEY` / `ADMIN_PASSWORD` / `CSP_SERVICE_TOKEN` / DB password / `INTERNAL_PLATFORM_API_KEY` / `CODESERVER_PASSWORD` are dev defaults (empty `SECRET_KEY` is always fatal); `ANILA_ALLOW_DEV_SECRET=1` downgrades to warnings.
- **Credential encryption**: AES-256-GCM (anila-core `credential_crypto` / `service_token_envelope`).
- **Token revocation**: durable `token_revocations` + JWT `tv` enforcement + Redis fan-out; `/api/auth/revocations` for cold-start sync.
- **Inbound hardening**: CORS allowlist (no `*` fallback), optional TrustedHostMiddleware, double-submit CSRF, SPA path-traversal guard, nginx security headers + rate-limit.

---

## Related docs

- Ingestion platform design: [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md) · Parent-child RAG: [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md)
- Multi-service integration: [`../docs/platform/multi-service-integration-plan.md`](../docs/platform/multi-service-integration-plan.md) · Service-token cutover: [`../docs/runbooks/service-token-cutover.md`](../docs/runbooks/service-token-cutover.md)
- anila-studio extraction: [`../docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md)
- Platform: [`../README.md`](../README.md) · Roadmap: [`../anila_plan.md`](../anila_plan.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)

---

**Role**: Control + Data Plane · **Authoritative for**: users · api_keys · models · agents · service_clients · token_usage · audit_logs · ingestion KB (Studio + FLUX + diagram rendering extracted to anila-studio; CSP keeps only the contract endpoints)
