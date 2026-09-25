# anila-studio

> The **content-generation service** extracted from `myCSPPlatform/backend` — the redesign's "Artifact Center / production engine" (`產出中心`): RAG finds your uploaded documents → an LLM drafts content → it renders multiple artifact types. Originally slides-only, it has grown into **five artifacts**: slides, reports, mindmaps, infographics, datatables.

> 中文版本：[README.md](README.md)

> 🌿 **Branch note**: This service exists on most deployment branches; the slim `trial-military` build does not include it. See the root [`README.md`](../../README.md) branch matrix (current line is a single `main`; the old seven-branch model is retired).

---

## Where it sits in the monorepo (post-redesign §17.1 layout)

```
services/anila-studio/         ← this service (FastAPI, port 8100)
services/pptx-renderer/        ← downstream Node renderer (.pptx / screenshots / geometric QA)
apps/anilalm/                  ← the "My Knowledge Base" + studio frontend, calling this via openapi codegen
infra/compose/platform.yml     ← compose definition (root compose.yaml is a shim that includes it)
```

Bring-up goes through the root compose shim: `compose.yaml` → `infra/compose/platform.yml` (project `anila`); `compose.dev.yaml` → `infra/compose/dev.yml`. Deployment scripts live under `infra/deployment/{scripts,intranet}/`.

### Why standalone

- **Change render / image logic without rebuilding csp**: dev loop drops from minutes to seconds.
- **Single responsibility**: csp is the governance center (auth / models / ingestion / proxy billing); anila-studio only generates content.
- **HTTP-only outward**: talks to csp via `csp_client` over HTTP, sharing **no DB**. The service does not import `anila_core`.

Service version **`0.1.0`** (`pyproject.toml` / `config.APP_VERSION` / `/health` agree).

---

## What the redesign added (Slices relevant to this service)

| Slice capability | Where it lands here |
|---|---|
| **Artifact contract + Redis job store** | `job_store.py`: a `PersistedJob` projection is written to Redis (key prefix `anila-studio:jobs:`, 7-day TTL) so a **restarted** studio can still answer status queries for pre-restart jobs; best-effort — a Redis outage degrades to in-memory only and does NOT block startup. `job_reporting.py`: reports to CSP via `POST /v1/artifact-jobs` (create), `PATCH /v1/artifact-jobs/{id}` (terminal / progress), `POST /v1/artifacts` (artifact landed). |
| **Full Trace spans + `/v1/traces` ingest** | `studio_trace.py`: `producer:"studio"`, one root `studio.job` span + one `studio.stage` span per pipeline step, batched to `POST {csp}/v1/traces/{trace_id}/spans` (≤256 spans/batch). No `trace_id` → the emitter is a full no-op; ship failure is drop-and-log and never breaks generation. |
| **Task spine (`task_id`)** | The create-job request payload carries `task_id` / `source_snapshot_id` / `trace_id`; `job_lifecycle.py`'s `JobReportContext` threads them through all five pipelines' `*JobUpdater` and passes them on to CSP with the artifact-job / artifact / trace spans. |
| **Four-level classification (passthrough)** | `classification_level` (`無機密` / `營業秘密` / `密` / `機密`) is carried by `PersistedJob`, returned from `POST /v1/artifacts`, and written into trace span attributes; studio never latches/declassifies, it only inherits and forwards. |
| **Model Gateway** | All LLM traffic goes through the CSP `POST /v1/chat/completions` proxy (keeping token billing); studio never talks to a model directly. |
| **JWKS / revocation auth** | `jwks_client` (fetch csp JWKS + cache) + `revocation_cache` (Redis pub/sub + cold-start, **fail-closed**). |

> (2), (3) and the CSP reporting are all **fire-and-forget** (retry-once, log-not-raise): CSP being down must never break generation. `STUDIO_ARTIFACT_REPORTING=false` silences the whole group (spans included). The cross-cutting coordination lives in `job_lifecycle.py`; the pipelines themselves are unchanged.

---

## Stack

- Python `>=3.11`, runtime image `python:3.11-slim`, port **8100** (compose only `expose`s it, not host-published).
- Core: FastAPI `>=0.110,<1.0`, uvicorn[standard] `>=0.27`, httpx `>=0.27,<1.0`, python-jose[cryptography] `>=3.3` (RS256/JWKS), pydantic `>=2.6` + pydantic-settings `>=2.2`, redis `>=5.0`, cachetools `>=5.3`, numpy `>=1.26`, opencc-python-reimplemented `>=0.1.7` (Simplified→Traditional).
- **Artifact render stack**: jinja2 `>=3.1`, playwright `>=1.43` (headless Chromium → PDF), pypandoc `>=1.13` (HTML → DOCX), matplotlib `>=3.8`, openpyxl `>=3.1` (XLSX), markdown-it-py `>=3.0`, python-docx `>=1.1`.
- dev: pytest, pytest-asyncio, respx, fakeredis, freezegun.
- Dockerfile system packages: `graphviz`, `pandoc`, `curl`, `wget`, `ca-certificates`, `fonts-noto-cjk` + Chromium/Playwright dep libs + `fontconfig`; the build downloads single-face Noto Sans CJK TC OTFs (Regular + Bold) and runs `playwright install chromium` into `/opt/playwright-browsers`; runs as non-root user `anila` (uid 10001).

---

## Layout

```
services/anila-studio/
├── pyproject.toml          # core + artifact render stack
├── Dockerfile              # python:3.11-slim + graphviz/pandoc/chromium/noto-cjk + non-root(anila 10001)
├── scripts/export-openapi.py    # regenerate openapi/studio.openapi.json (the only script)
├── openapi/studio.openapi.json  # 3.1.0; codegen source for ANILALM
├── app/
│   ├── main.py             # FastAPI lifespan (JWKS + revocation + JobStore) + /health readiness gate; registers 5 routers
│   ├── config.py · auth.py # env settings; RS256 JWT verify via JWKS + revocation cache
│   ├── api/                # studio.py reports.py mindmaps.py infographics.py datatables.py
│   ├── clients/csp_client.py    # wraps the csp HTTP API
│   ├── schemas/            # studio.py report.py mindmap.py infographic.py datatable.py
│   ├── services/           # 30 modules (grouped below)
│   └── templates/          # infographic/base.html.j2 + report/*.html.j2
└── tests/                  # 42 test files; 524 collected, 521 green, 3 pre-existing FLUX reds (see Testing)
```

`services/` groups (30 modules):
- **Slide pipeline**: `studio_config` / `studio_retrieval` / `studio_llm` / `studio_render` / `studio_vision_qa` / `studio_layout` / `studio_job_service` / `studio_text_normalizer` (s2twp Simplified→Traditional + cleanup) / `llm_json` (lenient JSON parsing).
- **FLUX image gen**: `flux_image_provider` / `flux_prompt_rewriter` / `flux_quality_gate` (VLM ranking + FFT striping) / `flux_style` / `diagram_renderer` (Graphviz dot→PNG) / `geometric_qa`.
- **Other artifacts**: `report_job_service` / `report_renderer` / `report_runner`, `mindmap_job_service` / `mindmap_renderer`, `infographic_job_service` / `infographic_renderer`, `datatable_job_service` / `datatable_exporter`.
- **Cross-cutting job coordination (new)**: `job_lifecycle` (`JobReportContext` + `*JobUpdater` coordination) / `job_store` (Redis `PersistedJob`) / `job_reporting` (CSP artifact reporting) / `studio_trace` (span emitter).
- **Auth / infra**: `jwks_client` (fetch csp JWKS + cache) / `revocation_cache` (Redis pub/sub + cold-start, fail-closed).

---

## Endpoints (five artifacts, all async-job mode)

| Artifact | Paths | Download formats |
|---|---|---|
| Slides | `POST /api/studio/slides/jobs` (202) · `GET /…/{id}` · `GET /…/{id}/pptx` · `DELETE /…/{id}` | `.pptx` |
| Reports | `POST /api/reports/jobs` · `GET /…/{id}` · `GET /…/{id}/download/{fmt}` · `DELETE` | `html` / `pdf` / `docx` |
| Mindmaps | `POST /api/mindmaps/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `svg` / `dot` |
| Infographics | `POST /api/infographics/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `html` / `pdf` |
| Datatables | `POST /api/datatables/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `html` / `csv` / `xlsx` |

Plus `GET /health`. `openapi/studio.openapi.json` (3.1.0) already covers all five families.

---

## Run it

```bash
cd services/anila-studio
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                                    # tests (no docker needed)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100     # needs csp:8000 / redis / flux2-dev / pptx-renderer
# or (from repo root): docker compose up -d --build anila-studio   # → infra/compose/platform.yml
```

Health: `curl http://localhost:8100/health` → `{"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,"deps":{"revocation_cache":true}}`. During lifespan startup it returns 503 + `ready=false` (status `"degraded"`) until JWKS + revocation cache cold-start finish. The JobStore is best-effort and does not gate readiness.

### Testing (524 collected · 521 green · 3 pre-existing reds)

`.venv/bin/python -m pytest` collects **524** tests (42 files): **521 pass**, **3 pre-existing FLUX reds** — all in `tests/test_hydrate_images.py` (`test_hydrate_image_prompt_calls_flux` / `test_cover_hero_path_generates_via_rewriter` / `test_mixed_slides_all_resolved`), a known pre-existing failure, not a regression from this work. Tests need no docker (csp / redis are mocked with respx / fakeredis).

---

## Dependency on csp

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/jwks.json` | fetch RS256 public key to verify JWTs |
| `GET /api/auth/revocations?since=` | cold-start sync of the revocation list at startup |
| `GET /api/ingestion/collections/{id}` | collection metadata |
| `POST /api/ingestion/collections/{id}/search` | RAG chunk retrieval |
| `POST /api/ingestion/collections/{id}/images/search` | RAG image retrieval |
| `GET /api/ingestion/images/{id}/blob` | raw image bytes |
| `POST /v1/chat/completions` | LLM (via csp proxy for billing; **no `/api/proxy` prefix**) |
| `POST /v1/artifact-jobs` · `PATCH /v1/artifact-jobs/{id}` · `POST /v1/artifacts` | artifact-job / artifact reporting (Slice 8b, fire-and-forget) |
| `POST /v1/traces/{trace_id}/spans` | Full Trace span ingest (producer `studio`, fire-and-forget) |

It also talks directly to the downstream `pptx-renderer` (`{RENDERER_BASE_URL}/render` · `/screenshots` · `/qa-geometric`) and the FLUX backend. CSP-reporting / trace auth reuses the user's bearer JWT (CSP re-verifies with RS256 + JWKS, preserving on-behalf-of semantics); if a legacy `CSP_SERVICE_TOKEN` is set it additionally attaches `X-CSP-Service-Token`.

### Redis pub/sub

Subscribes to channel `anila:auth:token-revoke` (csp publishes). On Redis loss the revocation cache is **fail-closed**: `/health` 503 + every auth-requiring endpoint 503; degrading to "TTL-only" is not allowed. (The JobStore uses the same Redis but only degrades to in-memory on loss — it is not fail-closed.)

---

## Key environment variables (from `config.py`)

| Variable | Default | Notes |
|---|---|---|
| `APP_NAME` / `APP_VERSION` / `LOG_LEVEL` | `anila-studio` / `0.1.0` / `INFO` | service identity / log |
| `CSP_BASE_URL` | `http://csp:8000` | the csp governance center |
| `CSP_SERVICE_TOKEN` | `""` | optional s2s token; when set, cold-start / reporting attach `X-CSP-Service-Token` |
| `REDIS_URL` / `REDIS_REVOCATION_CHANNEL` | `redis://redis:6379/0` / `anila:auth:token-revoke` | shared Redis + revocation channel with csp |
| `JWT_KID` / `JWT_ALGORITHMS` / `JWT_LEEWAY_SECONDS` | `anila-v1` / `("RS256",)` / `60` | JWT settings |
| `JWKS_REFRESH_SECONDS` / `REVOCATION_CACHE_TTL_SECONDS` | `3600` / `2592000` (30 days) | JWKS refetch / revocation deny-list TTL |
| `INTERNAL_TIMEOUT_SECONDS` / `INTERNAL_TIMEOUT_CONNECT` / `INTERNAL_LLM_TIMEOUT_SECONDS` | `30.0` / `5.0` / `300.0` | csp_client read / connect / long LLM timeout |
| `FLUX_BACKEND_URL` / `RENDERER_BASE_URL` | `http://flux2-dev:8000` / `http://pptx-renderer:7100` | FLUX backend (OpenAI-compatible Images API base URL, server root or with `/v1`) / pptx renderer |
| `FLUX_CACHE_DIR` | `/var/anila/anila-studio-flux-cache` | FLUX cache |
| `ARTIFACTS_DIR` | `/var/anila/anila-studio-artifacts` | **persistence root for report/mindmap/infographic/datatable outputs**; download endpoints read back from here |
| `JOB_STORE_KEY_PREFIX` / `JOB_STORE_TTL_SECONDS` | `anila-studio:jobs:` / `604800` (7 days) | Redis JobStore key prefix / TTL |
| `STUDIO_ARTIFACT_REPORTING` | `true` | master switch for CSP artifact-job / artifact / trace-span reporting |

> Note: some render paths read `os.environ` directly (`geometric_qa.py` reads `RENDERER_BASE_URL`, `studio_render.py` uses `FLUX_BACKEND_URL`); four FLUX knobs are **env-only, not in config.py**: `FLUX_MODEL` (the Images API `model` field, default `flux.2-dev`), `FLUX_API_KEY` (Bearer sent only when set), `FLUX_MAX_CONCURRENT`, `FLUX_TIMEOUT_SECONDS`. In compose an empty `FLUX_BACKEND_URL` disables studio image generation (no FLUX on the intranet). Since 2026-07 FLUX calls use the OpenAI-compatible `POST {base}/v1/images/generations` (`{model, prompt, n, size, response_format:"b64_json"}` → `{created, data:[{b64_json}]}`).

---

## How the frontend (ANILALM) calls it

`apps/anilalm/src/api/studio.ts` points at anila-studio via `VITE_STUDIO_BASE_URL`. TypeScript types are codegen'd from `openapi/studio.openapi.json`:

```bash
cd services/anila-studio && .venv/bin/python scripts/export-openapi.py   # re-export openapi after schema changes
cd ../../apps/anilalm && npm run gen:studio-types                        # → scripts/gen-studio-types.sh
```

---

## Deployment notes

- On the csp side the fixed `secrets/jwt-private.pem` path must exist (pre-generated by csp's deploy script or injected from Vault); anila-studio needs no private key, only reachable csp `/.well-known/jwks.json`.
- anila-studio fails fast at startup if csp `/api/auth/revocations` is unreachable (revocation cache is fail-closed).
- **`docker restart` does not reload `.env`/compose; apply config with `up -d`.**

---

## Related docs

- Redesign design lineage (convergence record): [`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) (`00-product-constitution.md`, `09-api-event-contracts.md` artifact / trace contracts, `02-system-architecture.md` JobStore failure model). Current authority: [`PLAN.md`](../../PLAN.md) (state + order of work); spec: [`SYSTEM-MAP.md`](../../SYSTEM-MAP.md).
- Studio / FLUX main spec: [`../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)
- Platform overview: [`../../README.md`](../../README.md) · current `main` (old seven-branch model retired)
