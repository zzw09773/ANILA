# anila-studio

> The **content-generation service** extracted from `myCSPPlatform/backend`: RAG finds your uploaded documents → an LLM drafts content → it renders multiple artifact types. Originally slides-only, it has grown into **five artifacts**: slides, reports, mindmaps, infographics, datatables.

> 中文版本：[README.md](README.md)

> 🌿 **Branch note**: This service exists on `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`. **The `trial-military` slim build does not include it.** See the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md).

---

## Why standalone

- **Change render / image logic without rebuilding csp**: dev loop drops from minutes to seconds.
- **Single responsibility**: csp is the control plane (auth / models / ingestion / proxy billing); anila-studio only generates content.
- **HTTP-only outward**: talks to csp via `csp_client` over HTTP, sharing **no** DB.

Service version **`0.1.0`** (`pyproject.toml` / `config.APP_VERSION` / `/health` agree). Extraction decision: [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md) (2026-05-23 / PR #12).

---

## Stack

- Python `>=3.11`, runtime image `python:3.11-slim`, port **8100**.
- Core: FastAPI `>=0.110,<1.0`, uvicorn[standard] `>=0.27`, httpx `>=0.27,<1.0`, python-jose[cryptography] `>=3.3` (RS256/JWKS), pydantic `>=2.6` + pydantic-settings `>=2.2`, redis `>=5.0`, cachetools `>=5.3`, numpy `>=1.26`, opencc-python-reimplemented `>=0.1.7`.
- **Artifact render stack** (new): jinja2 `>=3.1`, playwright `>=1.43` (Chromium → PDF), pypandoc `>=1.13`, matplotlib `>=3.8`, openpyxl `>=3.1`, markdown-it-py `>=3.0`, python-docx `>=1.1`.
- Dev: pytest, pytest-asyncio, respx, fakeredis, freezegun.
- Dockerfile system packages: `graphviz`, `pandoc`, `curl`, `wget`, `ca-certificates`, `fonts-noto-cjk` + Chromium/Playwright shared libs + `fontconfig`; build-time download of Noto Sans CJK TC OTF fonts and `playwright install chromium` into `/opt/playwright-browsers`; non-root user `anila` (uid 10001).

---

## Layout

```
anila-studio/
├── pyproject.toml          # core + artifact render stack
├── Dockerfile              # python:3.11-slim + graphviz/pandoc/chromium/noto-cjk + non-root
├── scripts/export-openapi.py    # regenerate openapi/studio.openapi.json (the only script)
├── openapi/studio.openapi.json  # 3.1.0; codegen for ANILALM
├── app/
│   ├── main.py             # FastAPI lifespan + /health readiness gate; registers 5 routers
│   ├── config.py · auth.py # env settings; RS256 JWT verify via JWKS + revocation cache
│   ├── api/                # studio.py reports.py mindmaps.py infographics.py datatables.py
│   ├── clients/csp_client.py    # wraps the csp HTTP API
│   ├── schemas/            # studio.py report.py mindmap.py infographic.py datatable.py
│   ├── services/           # 29 modules (grouped below)
│   └── templates/          # infographic/base.html.j2 + report/*.html.j2
└── tests/                  # 40 test files, ~451 test functions
```

`services/` groups (29 modules):
- **Slide pipeline**: `studio_config` / `studio_retrieval` / `studio_llm` / `studio_render` / `studio_vision_qa` / `studio_layout` / `studio_job_service` / `studio_text_normalizer` (Simplified→Traditional s2twp + cleanup) / `llm_json` (lenient JSON parsing).
- **FLUX imaging**: `flux_image_provider` / `flux_prompt_rewriter` / `flux_quality_gate` (VLM ranking + FFT striping) / `flux_style` / `diagram_renderer` (Graphviz dot→PNG) / `geometric_qa`.
- **Auth / infra**: `jwks_client` (fetch csp JWKS + cache) / `revocation_cache` (Redis pub/sub + cold-start, fail-closed).
- **Other artifacts**: `report_job_service` / `report_renderer` / `report_runner`, `mindmap_job_service` / `mindmap_renderer`, `infographic_job_service` / `infographic_renderer`, `datatable_job_service` / `datatable_exporter`.

---

## Endpoints (5 artifacts, all async-job)

| Artifact | Paths | Download formats |
|---|---|---|
| Slides | `POST /api/studio/slides/jobs` (202) · `GET /…/{id}` · `GET /…/{id}/pptx` · `DELETE /…/{id}` | `.pptx` |
| Reports | `POST /api/reports/jobs` · `GET /…/{id}` · `GET /…/{id}/download/{fmt}` · `DELETE` | `html` / `pdf` / `docx` |
| Mindmaps | `POST /api/mindmaps/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `svg` / `dot` |
| Infographics | `POST /api/infographics/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `html` / `pdf` |
| Datatables | `POST /api/datatables/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `html` / `csv` / `xlsx` |

Plus `GET /health`. `openapi/studio.openapi.json` (3.1.0) already includes all five families.

---

## Running

```bash
cd anila-studio
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                            # tests (no docker)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100   # needs csp:8000 / redis:6379 / flux2-dev / pptx-renderer
# or: docker compose -f docker-compose-dev.yml up -d --build anila-studio
```

Health: `curl http://localhost:8100/health` → `{"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,"deps":{"revocation_cache":true}}`. During lifespan startup it returns 503 + `ready=false` (status `"degraded"`), going green only after JWKS + revocation cache cold-start.

---

## Dependency on csp

| Endpoint | Use |
|---|---|
| `GET /.well-known/jwks.json` | RS256 public key to verify JWT |
| `GET /api/auth/revocations?since=` | cold-start sync of the revocation list |
| `GET /api/ingestion/collections/{id}` | collection metadata |
| `POST /api/ingestion/collections/{id}/search` | RAG chunk retrieval |
| `POST /api/ingestion/collections/{id}/images/search` | RAG image retrieval |
| `GET /api/ingestion/images/{id}/blob` | raw image bytes |
| `POST /v1/chat/completions` | LLM (via csp proxy to preserve billing; **note: no `/api/proxy` prefix**) |

It also calls the external `pptx-renderer` (`{RENDERER_BASE_URL}/render` / `/screenshots` / `/qa-geometric`) and the FLUX backend.

### Redis pub/sub

Subscribes to channel `anila:auth:token-revoke` (published by csp). On Redis loss it is **fail-closed**: `/health` 503 + every auth-requiring endpoint 503; no "degrade to TTL-only" (plan v2 R14).

---

## Key environment variables (from `config.py`)

| Variable | Default | Notes |
|---|---|---|
| `APP_NAME` / `APP_VERSION` / `LOG_LEVEL` | `anila-studio` / `0.1.0` / `INFO` | identity / log |
| `CSP_BASE_URL` | `http://csp:8000` | csp control plane |
| `CSP_SERVICE_TOKEN` | `""` | optional s2s token; if set, sent as `X-CSP-Service-Token` on the revocations cold-start |
| `REDIS_URL` / `REDIS_REVOCATION_CHANNEL` | `redis://redis:6379/0` / `anila:auth:token-revoke` | shared Redis + revoke channel |
| `JWT_KID` / `JWT_ALGORITHMS` / `JWT_LEEWAY_SECONDS` | `anila-v1` / `("RS256",)` / `60` | JWT settings |
| `JWKS_REFRESH_SECONDS` / `REVOCATION_CACHE_TTL_SECONDS` | `3600` / `2592000` (30 days) | JWKS re-fetch / revocation deny-list TTL |
| `INTERNAL_TIMEOUT_SECONDS` / `INTERNAL_TIMEOUT_CONNECT` / `INTERNAL_LLM_TIMEOUT_SECONDS` | `30.0` / `5.0` / `300.0` | csp_client read / connect / long LLM timeout |
| `FLUX_CACHE_DIR` | `/var/anila/anila-studio-flux-cache` | FLUX cache |
| `ARTIFACTS_DIR` | `/var/anila/anila-studio-artifacts` | **persistence root for report/mindmap/infographic/datatable outputs** |
| `FLUX_BACKEND_URL` / `RENDERER_BASE_URL` | `http://flux2-dev:8000` / `http://pptx-renderer:7100` | FLUX backend / pptx renderer |

> Note: although `FLUX_BACKEND_URL` / `RENDERER_BASE_URL` are in `config.py`, the render paths mostly read them via `os.environ` (`studio_render.py` reads `FLUX_BACKEND_URL`, `geometric_qa.py` reads `RENDERER_BASE_URL`, `studio_config.py` also hardcodes the renderer URL). Two FLUX knobs are **env-only, not in config.py**: `FLUX_MAX_CONCURRENT` (4), `FLUX_TIMEOUT_SECONDS` (180).

---

## How the frontend (ANILALM) calls it

`ANILALM/src/api/studio.ts` points at anila-studio via `VITE_STUDIO_BASE_URL`. TypeScript types are codegen'd from `openapi/studio.openapi.json`:

```bash
cd anila-studio && .venv/bin/python scripts/export-openapi.py   # after a schema change
cd ../ANILALM && npm run gen:studio-types
```

---

## Deployment notes

- csp's `JWT_PRIVATE_KEY_PATH` must exist (pre-generated via csp `scripts/generate-jwt-keypair.py` or Vault-injected); anila-studio needs no private key, only reachable csp `/.well-known/jwks.json`.
- anila-studio fails fast on startup if csp `/api/auth/revocations` is unreachable.
- `ALLOW_AUTO_KEYGEN` is dev/test only.

---

## Related docs

- Extraction plan / E2E: `docs/superpowers/anila-studio/plans/`
- Studio / FLUX spec: [`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- Platform: [`../README.md`](../README.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
