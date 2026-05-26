# ingestion-worker

> ANILA's async document-ingestion worker: runs parse → chunk → embed → index for a single document, and provides chunking-strategy evaluation (evaluator).

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** Worker contents identical to main (not in the fork zone); on prod, `INTERNAL_PLATFORM_API_KEY` is fail-loud.

## Overview

`ingestion-worker` is the background processor of the ANILA ingestion pipeline. It is **not an HTTP API**; it is a worker process driven by [Arq](https://arq-docs.helpmanual.io/) (a Redis-backed async job queue). The CSP backend enqueues an uploaded document as a job; the worker dequeues it and runs the full ingestion flow:

1. **Parse** — extract plain text from the uploaded blob (PDF / DOCX / ODF / RTF, etc., via the `anila-core` parser stack).
2. **Image caption (optional)** — the parser leaves `[[IMAGE:<id>]]` placeholders in the text; when a VLM endpoint is configured, the worker calls a vision model and rewrites those placeholders with captions so charts/diagrams become searchable text.
3. **Chunk** — split text using the chunker named by the collection's `chunking_config` (e.g. `hierarchical` / semantic).
4. **Embed** — batch-embed leaf chunks via an OpenAI-compatible embedding endpoint.
5. **Index** — write parent / leaf chunks plus vectors into PostgreSQL + pgvector (an agent/collection-scoped store).

Throughout, it updates `ingestion_documents` and `ingestion_jobs` status (queued → running → parsing → chunking → embedding → indexing → succeeded/failed), which CSP uses to push SSE progress to the frontend.

The worker also exposes a second handler, `evaluate_strategies`: given a set of sample documents + queries, it compares multiple chunking strategies on retrieval quality (Hit@1 / Hit@5 / MRR, plus an optional LLM-as-judge `judge_avg`) and writes back `recommended_strategy`.

> Note: this service has no HTTP health endpoint; liveness is reflected by the Arq worker process itself and by `ingestion_jobs` progress.

## Architecture & Stack

- **Language / runtime**: Python `>=3.11`; packaged with `hatchling`, source under `src/ingestion_worker`.
- **Job queue**: `arq>=0.26` (Redis-backed). Entrypoint is `arq ingestion_worker.main.WorkerSettings`.
- **Shared SDK**: `anila-core[rag]>=0.14.0` — supplies the parser registry (`pymupdf4llm`, `python-docx`, `odfpy`, `striprtf`, `Pillow`), chunking plugins, `PgPool`, `CollectionScopedPgVectorStore`, `VisionProvider`, the error taxonomy (`IngestionError` family), and security helpers (`decrypt_credential`, `validate_outbound_url`).
- **DB / vectors**: `asyncpg>=0.29` + `pgvector>=0.3`, writing to csp-db; the chunk vector column is `halfvec(4000)` (see migration 0015).
- **HTTP client**: `httpx>=0.27`, used to call the embedding endpoint, VLM endpoint, and judge LLM endpoint.
- **Config**: `pydantic-settings>=2.0`, loaded from environment variables (`WorkerSettings`).
- **Consumes / produces**:
  - Consumes: jobs from the Redis queue, uploaded blobs in the shared `UPLOAD_DIR`, and csp-db tables `ingestion_documents` / `ingestion_collections` / `ingestion_eval_runs` / `user_llm_credentials`.
  - Produces: writes to `document_chunks` (parent + leaf), `ingestion_images`, updates to `ingestion_documents` / `ingestion_jobs` / `ingestion_collections` counters, and backfilled `ingestion_eval_runs` results.

### Embedding dimension contract (important)

The embedding endpoint is expected to return NV-embed-V2's native 4096-d output; it does not support the OpenAI `dimensions` truncation parameter, so the worker truncates **client-side** to `EMBEDDING_DIM` (default 4000, matching the `halfvec(4000)` schema) and asserts the dimension on every response — so a wrong-dim INSERT fails fast here rather than with a less helpful asyncpg error.

## Layout

```
ingestion-worker/
├── Dockerfile            # build context is repo root; installs anila-core[rag] first, then this worker
├── pyproject.toml        # package definition, deps, ruff/pytest config
├── src/ingestion_worker/
│   ├── main.py           # Arq entrypoint: WorkerSettings, on_startup/on_shutdown, retry policy
│   ├── settings.py       # settings loaded from env (DB / Redis / embedding / vision)
│   ├── handlers.py       # ingest_document handler: parse→caption→chunk→embed→index main flow
│   ├── embedder.py       # OpenAI-compatible embedding client (with dim truncation + assert)
│   ├── evaluator.py      # evaluate_strategies handler: compares chunking strategies on retrieval quality
│   ├── judge.py          # LLM-as-judge scoring (credential decrypt + SSRF guard + 1–3 relevance)
│   └── parsers.py        # compat re-export of anila_core.ingestion.parsers.extract_text
└── tests/
    └── test_uniform_color.py   # unit test for the uniform-color (PDF background fill) image filter
```

## Setup & Run

### In the dev stack (recommended)

The service is named `ingestion-worker`, defined in the repo-root `docker-compose-dev.yml`:

- **build**: context is the repo root, dockerfile is `ingestion-worker/Dockerfile` (installs `anila-core[rag]` first, then the worker, layered for cache reuse).
- **depends_on** (all require `service_healthy`): `csp-db`, `redis`, `csp`.
- **volumes**: `./share-dev/uploads/ingestion` mounted to `/var/anila/ingestion-uploads` in the container (shared upload dir with CSP).
- **container CMD**: `arq ingestion_worker.main.WorkerSettings` (baked into the Dockerfile; `--watch` is intentionally off — dev uses explicit restarts).

Bring it up (adjust to your actual compose usage):

```bash
docker compose -f docker-compose-dev.yml up -d --build ingestion-worker
```

### Local development / tests

```bash
cd ingestion-worker
pip install -e '.[dev]'          # installs the worker + pytest/respx/ruff
pytest                            # asyncio_mode=auto; testpaths=tests
ruff check src tests              # line-length=100, target py311
```

> A full end-to-end run still needs reachable Redis, csp-db, and an embedding endpoint; plain `pytest` only covers unit tests with no external dependency (e.g. `_is_uniform_color`).

### Key environment variables

| Variable | Default (settings.py) | Description |
|----------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN; **must** use the `csp_app` role so RLS applies |
| `REDIS_URL` | `redis://redis:6379` | Arq queue backend |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:7011/v1` (compose overrides to `http://csp:8000/v1`) | OpenAI-compatible embedding endpoint |
| `EMBEDDING_MODEL` | `nvidia/NV-embed-V2` | embedding model identifier |
| `EMBEDDING_API_KEY` | `not-set` (compose injects the internal platform key) | embedding endpoint Bearer token |
| `EMBEDDING_DIM` | `4000` | dimension after client-side truncation; must match `halfvec(4000)` |
| `EMBEDDING_TIMEOUT_SECONDS` | `30.0` | per-request embedding timeout |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | upload-blob dir shared with CSP |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | connection-pool size (also caps worker concurrency) |
| `ENABLE_IMAGE_CAPTIONS` | `true` | master switch for VLM caption injection |
| `VISION_URL` | `""` (compose sets `http://csp:8000/v1`) | OpenAI-compatible VLM endpoint; empty disables captioning |
| `VISION_MODEL` | `gemma4` | VLM model identifier |
| `VISION_API_KEY` | `not-set` | VLM endpoint Bearer token |
| `VISION_VERIFY_SSL` | `false` | verify the VLM endpoint's TLS |
| `VISION_CONCURRENCY` | `4` | max parallel VLM calls per ingest job |
| `VISION_TIMEOUT_SECONDS` | `60.0` | per-image VLM timeout |
| `VISION_MAX_IMAGE_BYTES` | `8 MiB` | skip captioning images larger than this (avoid VLM OOM) |

> `SECRET_KEY`, `ANILA_ALLOW_*`, etc. are consumed by `anila-core`'s security module (credential decryption, SSRF/HTTP endpoint allowlisting); compose injects dev defaults.

## Integration

- **CSP backend (`csp`)**: upstream. Enqueues uploaded documents as Arq jobs and polls `ingestion_jobs` for progress/results. **Both embedding and VLM calls route through CSP's `/v1` proxy** (`EMBEDDING_BASE_URL` / `VISION_URL` both point at `http://csp:8000/v1`); CSP's `proxy_service` writes `token_usage` rows (`request_type='embedding'` / `'judge'`) so usage metering is consolidated and the worker no longer meters itself. The worker authenticates to CSP with the `ingestion-worker` system API key (injected via `INTERNAL_PLATFORM_API_KEY_DEV` in compose, seeded at CSP startup).
- **csp-db (PostgreSQL + pgvector)**: connects as `csp_app` (non-superuser, RLS-enforced). Reads `ingestion_documents`/`ingestion_collections`/`ingestion_eval_runs`/`user_llm_credentials`; writes chunks, images, statuses, and counters.
- **Redis**: Arq queue backend, carrying the two job functions `ingest_document` and `evaluate_strategies`.
- **Shared upload dir**: CSP writes upload blobs, the worker reads them; the same host path is bind-mounted into both containers (dev: `./share-dev/uploads/ingestion`). Captioned images are also written to `<UPLOAD_DIR>/anila-images/<doc_id>/`.
- **Judge LLM (evaluator, optional)**: a user-provided LLM credential lives in `user_llm_credentials` (AES-encrypted); during an eval run the worker decrypts it just-in-time and calls the endpoint to score. Outbound requests are SSRF-guarded via `validate_outbound_url`, and the credential's `__repr__` masks the API key.

## Related docs

- [`docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md) — overall ingestion-platform design (including the evaluator §6.5 LLM-as-judge spec).
- [`docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md) — parent / leaf two-tier chunking and parent-child RAG design.
- [`docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md) — responsibility boundary of the `anila-core` shared SDK (parser / chunker / store / security helpers all originate here).

---

**Last updated**: 2026-05-26 (sync PR #16 + add prod banner; worker contents identical to main)
