# anila-studio

> [中文版](README.md) is the canonical source · this is the English summary

The **deck generation service** extracted from `myCSPPlatform/backend`: RAG over your uploaded documents → LLM outline → FLUX images → PPTX render.

## Why standalone

- **Iterate on slide / image logic without rebuilding csp** — dev loop drops from minutes to seconds
- **Single responsibility** — csp is the control plane (auth / models / ingestion / proxy billing); anila-studio only generates decks
- **HTTP-only to csp** — no shared database, no shared private key

## Layout

```
anila-studio/
├── pyproject.toml          # fastapi / httpx / jose / redis / cachetools / opencc / numpy
├── Dockerfile              # python:3.11-slim + apt graphviz + non-root user
├── app/
│   ├── main.py             # FastAPI lifespan + /health readiness gate
│   ├── auth.py             # RS256 JWT verify via JWKS + revocation cache check
│   ├── api/studio.py       # /api/studio/slides/jobs (4 endpoints)
│   ├── clients/csp_client.py        # thin async wrappers around csp HTTP
│   └── services/
│       ├── jwks_client.py           # pulls csp /.well-known/jwks.json + cache
│       ├── revocation_cache.py      # Redis pub/sub subscriber
│       ├── flux_image_provider.py   # FLUX backend client
│       ├── flux_quality_gate.py     # VLM ranking + striping detection (FFT)
│       ├── studio_job_service.py    # in-memory job state machine
│       └── ...
└── tests/                  # 269 passing tests
```

## Running

### Local dev

```bash
cd anila-studio
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                   # 269 passed, 3 known reds
.venv/bin/uvicorn app.main:app --port 8100        # needs csp/redis/flux/renderer
```

### Docker

```bash
docker compose -f docker-compose-dev.yml up -d --build anila-studio
curl http://anila-platform-dev-anila-studio-1:8100/health  # via container
```

## Dependencies on csp

| Endpoint                                          | Purpose                          |
|---------------------------------------------------|----------------------------------|
| `GET /.well-known/jwks.json`                      | RS256 public key for JWT verify  |
| `GET /api/auth/revocations?since=`                | Cold-start sync of revoke list   |
| `POST /api/ingestion/collections/{id}/search`     | RAG chunks                       |
| `POST /api/ingestion/collections/{id}/images/search` | RAG images                    |
| `GET  /api/ingestion/images/{id}/blob`            | Raw image bytes                  |
| `GET  /api/ingestion/collections/{id}`            | Collection metadata              |
| `POST /api/proxy/v1/chat/completions`             | LLM (via csp proxy for billing)  |

Redis channel subscribed: `anila:auth:token-revoke`.

## Auth model

- **RS256 + JWKS**: csp signs, anila-studio verifies with public key fetched from JWKS endpoint
- **Token revocation**: csp publishes `anila:auth:token-revoke` events to Redis; anila-studio caches a 30-day deny-list and consults it on every authenticated request
- **Fail-closed**: if Redis disconnects, `/health` returns 503 and authenticated endpoints return 503 (no degrade-to-TTL-only)

## ANILALM integration

Frontend uses `STUDIO_BASE_URL` env var (`VITE_STUDIO_BASE_URL`) to target anila-studio. TypeScript types come from `anila-studio/openapi/studio.openapi.json` via `openapi-typescript`:

```bash
cd ANILALM && npm run gen:studio-types
```

## See also

- Plan v2:`docs/superpowers/anila-studio/plans/2026-05-23-extraction-plan.md`
- ADR:`docs/superpowers/anila-studio/extraction-decision.md`
- E2E runbook:`docs/superpowers/anila-studio/plans/2026-05-23-e2e-runbook.md`
- Baseline:`anila-studio/MIGRATION_BASELINE.md`
