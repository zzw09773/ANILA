# ingestion-worker

> ANILA's asynchronous document-ingestion worker: parse → chunk → embed → index a document + cross-document relation extraction, plus chunking-strategy evaluation (evaluator).

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This worker exists on every ANILA deployment branch and is identical across branches. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Overview

`ingestion-worker` is the background processor of ANILA's ingestion pipeline — **not an HTTP API** (no health endpoint) — a worker process driven by [Arq](https://arq-docs.helpmanual.io/) (a Redis-backed async job queue). CSP enqueues jobs; the worker pulls and runs them. Entrypoint: `arq ingestion_worker.main.WorkerSettings`.

`main.py` registers **three** job functions:

1. **`ingest_document`** — full ingestion of one document:
   - **Parse** (pct 15) → **Caption + persist images** (pct 22, optional VLM; replaces `[[IMAGE:<id>]]` placeholders with captions, writes `ingestion_images` + caption embeddings) → **Chunk** (pct 30, per `chunking_config`, default `hierarchical`) → split parent / leaf → **Embed leaves** (pct 60) → **Index** (pct 85, `CollectionScopedPgVectorStore`, `add_parent_chunks` then `index_chunks`) → update counters.
   - **Relation extraction (best-effort, all toggleable)**: ① regex citation edges (`relations.py`) ② LLM edges (`llm_relations.py`) ③ embedding topic-similarity edges (`similarity_relations.py`).
2. **`evaluate_strategies`** — compares retrieval quality of multiple chunking strategies (Hit@1 / Hit@5 / MRR, optional LLM-as-judge `judge_avg` 1–3) over sample docs + queries, writing back `recommended_strategy` and `results`.
3. **`reresolve_collection_relations`** — collection-level relation re-extraction: re-parse every `indexed` doc, re-run rule / LLM / similarity edges (per-doc parse failures skipped).

Each job function takes a single argument (CSP enqueues via `pool.enqueue_job(<name>, <arg>)`, see `services/csp/app/services/ingestion_queue.py`):

```text
ingest_document(document_id)                    # ingest one document
evaluate_strategies(eval_run_id)                # evaluate one eval run
reresolve_collection_relations(collection_id)   # re-extract a whole collection's relations
```

Arq `WorkerSettings`: `max_tries=3`, `job_timeout=300s`, `keep_result=3600s`.

---

## Architecture & Stack

- Language / runtime: Python `>=3.11`; packaged with `hatchling`, source in `src/ingestion_worker`.
- Job queue: `arq>=0.26` (Redis backend).
- Shared SDK: `anila-core[rag]>=0.14.0` — parser registry, chunking plugins, `PgPool`, `CollectionScopedPgVectorStore`, `VisionProvider`, the `IngestionError` taxonomy, security helpers (`decrypt_credential` / `validate_outbound_url`).
- DB / vectors: `asyncpg>=0.29` + `pgvector>=0.3`, writing to csp-db; chunk vector column `halfvec(4000)` (migration 0015).
- HTTP client: `httpx>=0.27` (embedding / VLM / relation-LLM / judge).
- Settings: `pydantic-settings>=2.0` (`WorkerSettings`, env-loaded, `case_sensitive=False`).

### Vector dimension contract (important)

The embedding endpoint returns NV-embed-V2's native 4096 dims and doesn't support OpenAI `dimensions` truncation, so the worker truncates **client-side** to `EMBEDDING_DIM` (default 4000, aligned with `halfvec(4000)`) and asserts the dimension after each response (mismatch → `E_EMBED_DIM_MISMATCH`, fail-fast, rather than surfacing as a cryptic asyncpg INSERT error).

---

## Layout

```
services/ingestion-worker/
├── Dockerfile            # build context = repo root; installs anila-core[rag] first, then this worker
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq WorkerSettings + three job functions + on_startup/shutdown + retry
│   ├── settings.py       # env-loaded settings (DB / Redis / embedding / vision / relation / similarity)
│   ├── handlers.py       # ingest_document + reresolve_collection_relations main flow
│   ├── embedder.py       # OpenAI-compatible embedding client (dimension truncation + assert)
│   ├── evaluator.py      # evaluate_strategies handler
│   ├── judge.py          # LLM-as-judge scoring (credential decrypt + SSRF guard)
│   ├── relations.py      # regex citation edges (rule-based)
│   ├── llm_relations.py  # LLM relation extraction
│   ├── similarity_relations.py  # embedding topic-similarity edges
│   └── parsers.py        # compatibility re-export of anila_core.ingestion.parsers.extract_text
└── tests/                # 8 test files (parsers / uniform_color / llm_relations /
                          #   handlers_helpers / embedder / judge / settings / evaluator_metrics)
```

---

## Setup & Run

```bash
# In the stack (recommended) — repo-root compose
docker compose -f compose.dev.yaml up -d --build ingestion-worker

# Local dev / tests
cd services/ingestion-worker && pip install -e '.[dev]'
pytest            # asyncio_mode=auto; testpaths=tests
ruff check src tests
```

In compose: build context = repo root; `depends_on` (all `service_healthy`) `csp-db` / `redis` / `csp`; volume `./share-dev/uploads/ingestion` → container `/var/anila/ingestion-uploads`; CMD `arq ingestion_worker.main.WorkerSettings`.

### Environment variables (from `settings.py`)

| Variable | Default | Notes |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN, **must** use the `csp_app` role (RLS) |
| `REDIS_URL` | `redis://redis:6379` | Arq queue backend |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:7011/v1` (compose `http://csp:8000/v1`) | embedding endpoint |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | `nvidia/NV-embed-V2` / `not-set` | model / Bearer token |
| `EMBEDDING_DIM` / `EMBEDDING_TIMEOUT_SECONDS` | `4000` / `30.0` | truncated dim (aligned with halfvec(4000)) / timeout |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | upload blob dir shared with CSP |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | pool size (also caps concurrency) |
| `ENABLE_IMAGE_CAPTIONS` | `true` | master switch for VLM captioning |
| `VISION_URL` | `""` (compose `http://csp:8000/v1`) | VLM endpoint; empty disables captioning |
| `VISION_MODEL` / `VISION_API_KEY` / `VISION_VERIFY_SSL` | `gemma4` / `not-set` / `false` | VLM model / token / TLS verify |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` / `VISION_MAX_IMAGE_BYTES` | `4` / `60.0` / `8 MiB` | concurrency / timeout / skip caption above |
| `ENABLE_RELATION_LLM` | `true` | master switch for LLM relation extraction |
| `RELATION_LLM_URL` | `""` | empty disables LLM edges |
| `RELATION_LLM_MODEL` / `RELATION_LLM_API_KEY` / `RELATION_LLM_VERIFY_SSL` | `gemma4` / `not-set` / `false` | model / token / TLS |
| `RELATION_LLM_TIMEOUT_SECONDS` / `RELATION_LLM_MAX_CHARS` / `RELATION_LLM_MAX_CANDIDATES` | `120.0` / `12000` / `200` | timeout / input cap / candidate cap |
| `ENABLE_SIMILARITY_EDGES` | `true` | master switch for embedding similarity edges |
| `SIMILARITY_TOP_K` / `SIMILARITY_MIN` / `SIMILARITY_MAX_DOCS` | `3` / `0.75` / `500` | K nearest siblings per doc / cosine floor / skip recompute above |

> `SECRET_KEY`, `ANILA_ALLOW_*` are consumed by `anila-core`'s security modules (credential decrypt / SSRF); compose injects dev defaults.

---

## Integration

- **CSP**: upstream. Enqueues jobs + polls progress. **Embedding / VLM / relation-LLM calls all route through CSP's `/v1` proxy** (pointing at `http://csp:8000/v1`); CSP's `proxy_service` writes unified `token_usage`, the worker keeps no books of its own (the `user_id` passed to `embed()` is discarded with a `del`). It authenticates to CSP with the `ingestion-worker` system API key (injected by `INTERNAL_PLATFORM_API_KEY_DEV` in compose).
- **csp-db**: connects as `csp_app` (RLS-bound); RLS-scoped writes use `SET LOCAL anila.collection_id`. Reads documents / collections / eval_runs / user_llm_credentials; writes chunks / images / `document_relations` / state / counters.
- **Redis**: Arq queue backend.
- **Shared upload dir**: CSP writes, the worker reads; captioned images stored under `<UPLOAD_DIR>/anila-images/<doc_id>/`.
- **Judge / relation LLM**: the user's own credentials (`user_llm_credentials`, AES) are decrypted just-in-time; outbound calls are SSRF-guarded via `validate_outbound_url`, and credential `__repr__` masks the key.

---

## Related docs

- [`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md) (incl. evaluator §6.5 LLM-as-judge)
- [`../../docs/ingestion/parent-child-rag-design.md`](../../docs/ingestion/parent-child-rag-design.md)
- [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md)
- Platform: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
