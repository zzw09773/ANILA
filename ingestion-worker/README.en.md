# ingestion-worker

> ANILA's asynchronous document-ingestion worker: parse → chunk → embed → index a document, plus chunking-strategy evaluation (evaluator).

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This worker exists on every ANILA deployment branch and is identical across branches. See the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md).

---

## Overview

`ingestion-worker` is the background processor of ANILA's ingestion pipeline. It is **not an HTTP API**; it is a worker process driven by [Arq](https://arq-docs.helpmanual.io/) (a Redis-backed async job queue). The CSP backend enqueues uploaded documents as jobs; the worker pulls them and runs the full ingestion flow:

1. **Parse** — extract plain text from the uploaded blob (PDF / DOCX / ODF / RTF etc., via the `anila-core` parser stack).
2. **Image caption (optional)** — the parser leaves `[[IMAGE:<id>]]` placeholders; when a VLM endpoint is configured, the worker calls the vision model to write image captions back into the text so figures are retrievable too.
3. **Chunk** — split using the collection's `chunking_config` chunker (`hierarchical` / semantic).
4. **Embed** — batch-vectorize leaf chunks (calling an OpenAI-compatible embedding endpoint).
5. **Index** — write parent / leaf chunks plus vectors into PostgreSQL + pgvector (collection-scoped store).

It continuously updates `ingestion_documents` and `ingestion_jobs` state (queued → running → parsing → chunking → embedding → indexing → succeeded/failed), which CSP uses to push SSE progress to the frontend.

A second handler `evaluate_strategies` compares the retrieval quality of multiple chunking strategies (Hit@1 / Hit@5 / MRR, optionally LLM-as-judge `judge_avg`) over a sample set of documents + queries, then writes back `recommended_strategy`.

> No HTTP health endpoint; liveness is reflected by the Arq worker process and `ingestion_jobs` progress.

---

## Architecture & Stack

- **Language / runtime**: Python `>=3.11`; packaged with `hatchling`, source in `src/ingestion_worker`.
- **Job queue**: `arq>=0.26` (Redis backend). Entrypoint: `arq ingestion_worker.main.WorkerSettings`.
- **Shared SDK**: `anila-core[rag]>=0.14.0` — parser registry (`pymupdf4llm` / `python-docx` / `odfpy` / `striprtf` / `Pillow`), chunking plugins, `PgPool`, `CollectionScopedPgVectorStore`, `VisionProvider`, the `IngestionError` taxonomy, and security helpers (`decrypt_credential` / `validate_outbound_url`).
- **DB / vectors**: `asyncpg>=0.29` + `pgvector>=0.3`, writing to csp-db; chunk vector column is `halfvec(4000)` (migration 0015).
- **HTTP client**: `httpx>=0.27` (embedding / VLM / judge LLM endpoints).
- **Settings**: `pydantic-settings>=2.0` (`WorkerSettings`, loaded from env).

### Vector dimension contract (important)

The embedding endpoint is expected to return NV-embed-V2's native 4096 dims; it does not support the OpenAI `dimensions` truncation parameter, so the worker truncates **client-side** to `EMBEDDING_DIM` (default 4000, aligned with the `halfvec(4000)` schema) and asserts the dimension after each response, so a wrong dimension surfaces clearly rather than as a cryptic asyncpg INSERT error.

---

## Layout

```
ingestion-worker/
├── Dockerfile            # build context = repo root; installs anila-core[rag] first, then this worker
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq entrypoint: WorkerSettings, on_startup/on_shutdown, retry policy
│   ├── settings.py       # env-loaded settings (DB / Redis / embedding / vision)
│   ├── handlers.py       # ingest_document handler: parse→caption→chunk→embed→index main flow
│   ├── embedder.py       # OpenAI-compatible embedding client (with dimension truncation + assert)
│   ├── evaluator.py      # evaluate_strategies handler: compare chunking-strategy retrieval quality
│   ├── judge.py          # LLM-as-judge scoring (credential decrypt + SSRF guard + 1–3 relevance)
│   └── parsers.py        # compatibility re-export of anila_core.ingestion.parsers.extract_text
└── tests/
    └── test_uniform_color.py   # unit test for uniform-color image (PDF background fill) filtering
```

---

## Setup & Run

### In the stack (recommended)

Service name `ingestion-worker`, defined in the repo-root `docker-compose-dev.yml`: build context = repo root, dockerfile = `ingestion-worker/Dockerfile` (install `anila-core[rag]` first, then the worker); `depends_on` (all `service_healthy`): `csp-db` / `redis` / `csp`; volume `./share-dev/uploads/ingestion` → container `/var/anila/ingestion-uploads` (shared upload dir with CSP); CMD `arq ingestion_worker.main.WorkerSettings` (`--watch` deliberately off; dev uses explicit restarts).

```bash
docker compose -f docker-compose-dev.yml up -d --build ingestion-worker
```

### Local dev / tests

```bash
cd ingestion-worker
pip install -e '.[dev]'          # worker + pytest/respx/ruff
pytest                            # asyncio_mode=auto; testpaths=tests
ruff check src tests              # line-length=100, target py311
```

> A full run still needs a reachable Redis / csp-db / embedding endpoint; plain `pytest` only covers unit tests with no external dependency.

### Main environment variables

| Variable | Default (settings.py) | Notes |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN, **must** use the `csp_app` role for RLS |
| `REDIS_URL` | `redis://redis:6379` | Arq queue backend |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:7011/v1` (compose overrides to `http://csp:8000/v1`) | OpenAI-compatible embedding endpoint |
| `EMBEDDING_MODEL` | `nvidia/NV-embed-V2` | embedding model id |
| `EMBEDDING_DIM` | `4000` | client-side truncated dimension, must align with `halfvec(4000)` |
| `EMBEDDING_TIMEOUT_SECONDS` | `30.0` | per-embedding timeout |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | upload blob dir shared with CSP |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | pool size (also caps worker concurrency) |
| `ENABLE_IMAGE_CAPTIONS` | `true` | master switch for VLM caption injection |
| `VISION_URL` | `""` (compose `http://csp:8000/v1`) | OpenAI-compatible VLM endpoint; empty disables captioning |
| `VISION_MODEL` / `VISION_API_KEY` | `gemma4` / `not-set` | VLM model / Bearer token |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` | `4` / `60.0` | concurrent VLM calls per job / per-image timeout |
| `VISION_MAX_IMAGE_BYTES` | `8 MiB` | skip captioning above this (avoid VLM OOM) |

> `SECRET_KEY`, `ANILA_ALLOW_*` are consumed by `anila-core`'s security modules (credential decrypt / SSRF / HTTP endpoint allow); compose injects dev defaults.

---

## Integration

- **CSP backend (`csp`)**: upstream. Enqueues uploads as Arq jobs and polls `ingestion_jobs` progress. **Embedding and VLM calls are both routed through CSP's `/v1` proxy** (`EMBEDDING_BASE_URL` / `VISION_URL` point at `http://csp:8000/v1`); CSP's `proxy_service` writes unified `token_usage`, the worker keeps no books of its own. The worker authenticates to CSP with the `ingestion-worker` system API key (injected by `INTERNAL_PLATFORM_API_KEY_DEV` in compose and seeded at CSP startup).
- **csp-db (PostgreSQL + pgvector)**: connects as `csp_app` (non-superuser, RLS-bound). Reads `ingestion_documents` / `ingestion_collections` / `ingestion_eval_runs` / `user_llm_credentials`; writes chunks / images / state / counts.
- **Redis**: Arq queue backend, carrying the `ingest_document` and `evaluate_strategies` job functions.
- **Shared upload dir**: CSP writes, the worker reads; the same host path bind-mounted into both containers (dev: `./share-dev/uploads/ingestion`). Captioned images go to `<UPLOAD_DIR>/anila-images/<doc_id>/`.
- **Judge LLM (optional evaluator)**: the user's own LLM credentials are stored in `user_llm_credentials` (AES-encrypted); the worker decrypts and scores at eval time; outbound calls are SSRF-guarded via `validate_outbound_url`, and credential `__repr__` masks the API key.

---

## Related docs

- [`../docs/ingestion/ingestion-platform-design.md`](../docs/ingestion/ingestion-platform-design.md) — overall ingestion platform design (incl. evaluator §6.5 LLM-as-judge spec)
- [`../docs/ingestion/parent-child-rag-design.md`](../docs/ingestion/parent-child-rag-design.md) — parent / leaf two-tier chunks and parent-child RAG
- [`../docs/anila-core/anila-core-boundary.md`](../docs/anila-core/anila-core-boundary.md) — `anila-core` shared SDK boundary
- Platform: [`../README.md`](../README.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
