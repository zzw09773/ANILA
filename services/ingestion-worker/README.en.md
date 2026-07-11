# ingestion-worker

> The async background worker for ANILA document ingestion: it runs one uploaded file through **parse → chunk → embed → index**, extracts cross-document relations, and ships a chunking-strategy evaluator. It is the engine behind the "My Knowledge Base" (`我的知識庫`) entry that actually turns documents into retrievable vectors.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This worker exists on every ANILA deployment branch with identical content. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Where it sits in the monorepo (post-redesign §17.1 layout)

The redesign collapsed the tree into four layers — `services/` · `apps/` · `packages/` · `infra/`. This service lives at:

```
services/ingestion-worker/     ← this service (Arq worker, no HTTP surface)
packages/anila-core/           ← shared SDK (parser / chunker / vector store / security helpers)
infra/compose/platform.yml     ← compose definition (root compose.yaml is a shim that includes it)
```

Bring-up always goes through the root compose shim: `compose.yaml` → `infra/compose/platform.yml` (prod stack, project `anila-platform`); `compose.dev.yaml` → `infra/compose/dev.yml` (dev stack). Deployment scripts live under `infra/deployment/{scripts,intranet}/`.

---

## This is not an HTTP service

`ingestion-worker` has **no HTTP endpoint and no health route**. It is a worker process driven by [Arq](https://arq-docs.helpmanual.io/) (a Redis-backed async job queue): CSP enqueues a job, the worker pulls it and runs it. Entry point:

```bash
arq ingestion_worker.main.WorkerSettings
```

`main.py`'s `WorkerSettings` registers **three** job functions (CSP dispatches them via `pool.enqueue_job(<name>, <arg>)`, see `services/csp/app/services/ingestion_queue.py`):

```text
ingest_document(document_id)                    # full ingest of one document
evaluate_strategies(eval_run_id)                # score one chunking eval run
reresolve_collection_relations(collection_id)   # re-extract a collection's relations
```

Arq retry / timeout policy (`main.py`): `max_tries=3`, `job_timeout=300` (s), `keep_result=3600` (so CSP can poll completion within an hour). `on_startup` opens one shared `PgPool` + builds an `Embedder`; `on_shutdown` drains both.

### `ingest_document` pipeline (with progress pct)

| pct | Stage | Notes |
|---|---|---|
| 5 | start | mark `running` |
| 15 | Parse | `anila_core.ingestion.parsers.extract_text` via the parser registry |
| 22 | Caption + land images | optional VLM: replace `[[IMAGE:<id>]]` placeholders with captions, write `ingestion_images` + caption embeddings |
| 30 | Chunk | split into parent / leaf per `chunking_config` (default `hierarchical`) |
| 60 | Embed leaf | one embedding call per chunk |
| 85 | Index | `CollectionScopedPgVectorStore`: `add_parent_chunks` then `index_chunks` |
| 100 | done | update counts, mark `indexed` |

**Relation extraction (all best-effort, all toggleable)** runs at the tail: (1) regex citation edges (`relations.py`), (2) LLM edges (`llm_relations.py`), (3) embedding topic-similarity edges (`similarity_relations.py`). `reresolve_collection_relations` re-runs all three across every `indexed` document in a collection (per-doc parse failure is skipped).

---

## Architecture & stack

- Language / runtime: Python `>=3.11`; packaged with `hatchling`, source under `src/ingestion_worker`.
- Job queue: `arq>=0.26` (Redis backend).
- Shared SDK: `anila-core[rag]>=0.14.0` — parser registry, chunking plugins, `PgPool`, `CollectionScopedPgVectorStore`, `VisionProvider`, the `IngestionError` taxonomy, and security helpers (credential decrypt `decrypt_credential`, SSRF guard `validate_outbound_url`). The `[rag]` extra pulls in the parser stack (pymupdf4llm, python-docx, odfpy, striprtf, Pillow).
- DB / vectors: `asyncpg>=0.29` + `pgvector>=0.3`, writing to csp-db; chunk vector column is `halfvec(4000)` (migration 0015).
- HTTP client: `httpx>=0.27` (embedding / VLM / relation-LLM / judge).
- Config: `pydantic-settings>=2.0` (`WorkerSettings`, env-loaded, `case_sensitive=False`).

### `.doc` needs antiword (installed in the Dockerfile)

Legacy `.doc` (binary Word) is converted to plain text by `anila-core`'s `DocParser`, which shells out to **antiword**; without it a `.doc` upload fails at parse with `antiword is required for .doc parsing`. The Dockerfile installs it via `apt-get install antiword`; `DocParser` invokes it as `["antiword", "--", file_path]`, using `--` to block argument injection from filenames starting with `-`.

### Vector-dimension contract (important)

The embedding endpoint returns NV-embed-V2's native 4096-d and does **not** honour OpenAI-style `dimensions` truncation, so the worker truncates **client-side** to `EMBEDDING_DIM` (default 4000, aligned to `halfvec(4000)` — halfvec HNSW caps at 4000-d) and asserts the dimension on every response. A mismatch raises `E_EMBED_DIM_MISMATCH` (fail-fast, instead of surfacing at the asyncpg INSERT).

---

## Directory layout

```
services/ingestion-worker/
├── Dockerfile            # build context = repo root; installs packages/anila-core[rag] then this worker; apt installs antiword
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq WorkerSettings + the three job functions + on_startup/shutdown + retry
│   ├── settings.py       # env-loaded settings (DB / Redis / embedding / vision / relation / similarity)
│   ├── handlers.py       # ingest_document + reresolve_collection_relations core flow
│   ├── embedder.py       # OpenAI-compatible embedding client (dim truncation + assert)
│   ├── evaluator.py      # evaluate_strategies handler
│   ├── judge.py          # LLM-as-judge scoring (credential decrypt + SSRF guard)
│   ├── relations.py      # regex citation edges (rule-based)
│   ├── llm_relations.py  # LLM relation extraction
│   ├── similarity_relations.py  # embedding topic-similarity edges
│   └── parsers.py        # compat re-export of anila_core.ingestion.parsers.extract_text
└── tests/                # 8 test files (parsers / uniform_color / llm_relations / handlers_helpers /
                          #   embedder / judge / settings / evaluator_metrics) — 144 tests total
```

---

## Run & test

```bash
# In the stack (recommended) — root compose shim
docker compose up -d --build ingestion-worker            # → infra/compose/platform.yml
# dev stack: docker compose -f compose.dev.yaml up -d --build ingestion-worker

# Local dev / test (install anila-core first, then this worker)
cd services/ingestion-worker
.venv/bin/python -m pytest         # 144 tests; asyncio_mode=auto; testpaths=tests
.venv/bin/ruff check src tests
```

> For a fresh venv, run this from the current `services/ingestion-worker`
> directory in Dockerfile order: `pip install -e ../../packages/anila-contracts -e ../../packages/anila-security -e '../../packages/anila-core[rag]' -e '.[dev]'`.

In compose (`infra/compose/platform.yml`): build context = repo root; `depends_on` (all `service_healthy`) `csp-db` / `redis` / `csp`; volume `share/uploads/ingestion` (host) → `/var/anila/ingestion-uploads`; CMD `arq ingestion_worker.main.WorkerSettings`; `restart: unless-stopped`. **`docker restart` does not reload `.env`/compose; apply config with `up -d`.**

### Environment variables (from `settings.py`; compose overrides noted)

| Variable | Default | Notes |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN; **must** be the `csp_app` role (RLS-bound, non-superuser) |
| `REDIS_URL` | `redis://redis:6379` | Arq queue backend |
| `EMBEDDING_BASE_URL` | `http://host.docker.internal:7011/v1` (compose `http://csp:8000/v1`) | embedding endpoint |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | `nvidia/NV-embed-V2` / `not-set` | model / Bearer token |
| `EMBEDDING_DIM` / `EMBEDDING_TIMEOUT_SECONDS` | `4000` / `30.0` | truncation dim (aligned to halfvec(4000)) / timeout |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | shared upload-blob dir with CSP |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | connection pool (also caps concurrency) |
| `ENABLE_IMAGE_CAPTIONS` | `true` | VLM caption master switch |
| `VISION_URL` | `""` (compose `http://csp:8000/v1`) | VLM endpoint; empty string disables captioning |
| `VISION_MODEL` / `VISION_API_KEY` / `VISION_VERIFY_SSL` | `gemma4` / `not-set` / `false` | VLM model / token / TLS verify |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` / `VISION_MAX_IMAGE_BYTES` | `4` / `60.0` / `8 MiB` | parallelism / timeout / skip caption above size |
| `ENABLE_RELATION_LLM` | `true` | LLM relation-extraction master switch |
| `RELATION_LLM_URL` | `""` | empty string disables LLM edges |
| `RELATION_LLM_MODEL` / `RELATION_LLM_API_KEY` / `RELATION_LLM_VERIFY_SSL` | `gemma4` / `not-set` / `false` | model / token / TLS |
| `RELATION_LLM_TIMEOUT_SECONDS` / `RELATION_LLM_MAX_CHARS` / `RELATION_LLM_MAX_CANDIDATES` | `120.0` / `12000` / `200` | timeout / input cap / candidate cap |
| `ENABLE_SIMILARITY_EDGES` | `true` | embedding similarity-edge master switch |
| `SIMILARITY_TOP_K` / `SIMILARITY_MIN` / `SIMILARITY_MAX_DOCS` | `3` / `0.75` / `500` | K nearest neighbours per doc / cosine floor / skip recompute above N |

> `SECRET_KEY`, `ANILA_ENV`, and `ANILA_ALLOW_*` are consumed by `anila-core` security modules (credential decrypt / SSRF / http-endpoint fail-closed); compose injects them from the environment.

---

## Relationship to other services

- **CSP (governance center)**: upstream. Enqueues jobs + polls progress. **Embedding / VLM / relation-LLM calls are all routed through the CSP `/v1` proxy** (compose points at `http://csp:8000/v1`), so CSP's `proxy_service` writes `token_usage` centrally and the worker never meters itself (the `user_id` handed to `embed()` is immediately `del`'d). It authenticates to CSP with the **`ingestion-worker` system API key** (a per-service Model Gateway key; compose injects `INTERNAL_PLATFORM_API_KEY`). Before every outbound call, `anila-core` applies http-endpoint fail-closed + SSRF checks keyed on `ANILA_ENV` / `ANILA_ALLOW_*`.
- **csp-db**: connects as `csp_app` (RLS-bound); RLS-scoped writes use `SET LOCAL anila.collection_id`. Reads documents / collections / eval_runs / user_llm_credentials; writes chunks / images / `document_relations` / status / counts.
- **Redis**: the Arq queue backend.
- **Shared upload dir**: CSP writes, worker reads; captioned images land in `<UPLOAD_DIR>/anila-images/<doc_id>/`.
- **User credentials for judge / relation LLM**: user-supplied credentials (`user_llm_credentials`, AES) are decrypted just-in-time; `validate_outbound_url` (SSRF) runs before egress, and the credential object's `__repr__` masks the key.

> This worker predates the redesign's Task spine / Full Trace / Artifact contract and **does not** participate in them: it reads no `X-ANILA-Task-Id`, emits no trace spans, and sets no classification level. Its only redesign touch-points are the §17.1 layout, the compose shim, and the CSP Model Gateway key + `ANILA_ENV` fail-closed egress guards.

---

## Related docs

- [`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md) (incl. evaluator §6.5 LLM-as-judge)
- [`../../docs/ingestion/parent-child-rag-design.md`](../../docs/ingestion/parent-child-rag-design.md)
- [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md)
- Redesign design authority: [`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) (`00-product-constitution.md`, `02-system-architecture.md`)
- Platform overview: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
