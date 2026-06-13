# anila-studio

> The **deck-generation service** extracted from `myCSPPlatform/backend`: RAG finds your uploaded documents → LLM drafts an outline → FLUX generates illustrations → PPTX rendering.

> 中文版本：[README.md](README.md)

> 🌿 **Branch note**: This service exists on `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`. **The `trial-military` slim build does not include it** (Studio / FLUX deck generation removed). See the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md).

---

## Why standalone

- **Change illustration logic without rebuilding csp**: dev loop drops from minutes to seconds.
- **Single responsibility**: csp is the control plane (auth / models / ingestion / proxy billing); anila-studio only does deck generation.
- **HTTP-only outward**: talks to csp via `csp_client` over HTTP, sharing **no** DB.

Extraction history & decision: [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md) (2026-05-23 / PR #12).

---

## Layout

```
anila-studio/
├── pyproject.toml          # fastapi / httpx / jose / redis / cachetools / opencc / numpy
├── Dockerfile              # python:3.11-slim + apt graphviz + non-root user
├── scripts/
│   ├── export-openapi.py   # regenerate openapi/studio.openapi.json
│   └── generate-jwt-keypair.py  # csp-side dev helper (kept for reference)
├── openapi/studio.openapi.json  # contract for ANILALM codegen
├── app/
│   ├── main.py             # FastAPI lifespan + /health readiness gate
│   ├── config.py           # env settings (CSP_BASE_URL / REDIS_URL / FLUX_BACKEND_URL ...)
│   ├── auth.py             # RS256 JWT verify via JWKS + revocation cache check
│   ├── api/studio.py       # /api/studio/slides/jobs (4 endpoints)
│   ├── clients/csp_client.py    # 5 thin async functions wrapping the csp HTTP API
│   ├── services/
│   │   ├── jwks_client.py             # fetch csp /.well-known/jwks.json + cache
│   │   ├── revocation_cache.py        # Redis pub/sub subscribing to csp token revoke
│   │   ├── flux_image_provider.py     # FLUX backend client + image cache
│   │   ├── flux_quality_gate.py       # VLM ranking + striping detection (FFT)
│   │   ├── flux_prompt_rewriter.py · flux_style.py
│   │   ├── studio_job_service.py      # in-memory job state machine
│   │   ├── studio_text_normalizer.py  # Simplified→Traditional + text cleanup
│   │   ├── diagram_renderer.py        # Graphviz dot → PNG
│   │   └── geometric_qa.py            # slide geometry QA
│   └── schemas/studio.py   # pydantic SlidesSpec / JobStatus / GenerateSpecRequest etc.
└── tests/                  # 91 tests (own + ported from csp)
```

---

## Running

### Local dev

```bash
cd anila-studio
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                            # tests (no docker needed)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100   # needs csp:8000 / redis:6379 / flux2-dev / pptx-renderer
```

### Docker

```bash
docker compose -f docker-compose-dev.yml up -d --build anila-studio
docker logs anila-platform-dev-anila-studio-1 -f
```

### Health check

```bash
curl http://localhost:8100/health
# {"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,"deps":{"revocation_cache":true}}
```

During lifespan startup `/health` returns 503 + `ready=false`, going green only after JWKS + revocation cache cold-start.

---

## Dependency on csp

| Endpoint | Use |
|---|---|
| `GET /.well-known/jwks.json` | fetch RS256 public key to verify JWT |
| `GET /api/auth/revocations?since=` | cold-start sync of the revocation list |
| `POST /api/ingestion/collections/{id}/search` | RAG chunk retrieval |
| `POST /api/ingestion/collections/{id}/images/search` | RAG image retrieval |
| `GET /api/ingestion/images/{id}/blob` | fetch raw image bytes |
| `GET /api/ingestion/collections/{id}` | collection metadata |
| `POST /api/proxy/v1/chat/completions` | LLM (via csp proxy to preserve billing) |

### Redis pub/sub

Subscribes to channel `anila:auth:token-revoke` (published by csp):

```json
{"user_id": 42, "revoked_at_version": 3, "ts": "...", "schema_version": 1}
```

On Redis loss anila-studio is **fail-closed**: `/health` 503 + every auth-requiring endpoint 503; no "degrade to TTL-only" (plan v2 R14).

---

## How the frontend (ANILALM) calls it

`ANILALM/src/api/studio.ts` points at anila-studio via `VITE_STUDIO_BASE_URL` (empty → vite dev proxy or nginx reverse-proxy). TypeScript types are codegen'd from `anila-studio/openapi/studio.openapi.json`:

```bash
cd anila-studio && .venv/bin/python scripts/export-openapi.py   # after a schema change
cd ../ANILALM && npm run gen:studio-types
```

---

## Key environment variables

| Variable | Default | Notes |
|---|---|---|
| `CSP_BASE_URL` | `http://csp:8000` | csp control plane |
| `REDIS_URL` | `redis://redis:6379/0` | shared Redis with csp |
| `REDIS_REVOCATION_CHANNEL` | `anila:auth:token-revoke` | Redis pub/sub channel |
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | FLUX backend |
| `RENDERER_BASE_URL` | `http://pptx-renderer:7100` | pptx renderer |
| `JWT_KID` / `JWT_ALGORITHMS` | `anila-v1` / `("RS256",)` | JWT kid + accepted algorithms |
| `JWKS_REFRESH_SECONDS` | `3600` | JWKS re-fetch interval |
| `REVOCATION_CACHE_TTL_SECONDS` | `2592000` | 30 days, aligned with csp retention |
| `INTERNAL_TIMEOUT_SECONDS` | `30.0` | csp_client HTTP timeout |
| `FLUX_CACHE_DIR` | `/var/anila/anila-studio-flux-cache` | FLUX cache dir |

---

## Deployment notes

- csp's `JWT_PRIVATE_KEY_PATH` must exist (pre-generated via `scripts/generate-jwt-keypair.py` or Vault-injected); anila-studio needs no private key, only reachable csp `/.well-known/jwks.json`.
- anila-studio fails fast on startup if csp `/api/auth/revocations` is unreachable — keep `depends_on: csp` aligned with the health gate.
- `ALLOW_AUTO_KEYGEN=true` is dev/test only — **never enable in prod**.

---

## Related docs

- Extraction plan: `docs/superpowers/anila-studio/plans/2026-05-23-extraction-plan.md` (plan v2)
- E2E runbook: `docs/superpowers/anila-studio/plans/2026-05-23-e2e-runbook.md`
- Studio / FLUX spec: [`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- Platform: [`../README.md`](../README.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
