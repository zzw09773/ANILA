# ANILA load test (k6) — pool-fix concurrency probe

Reclaimed from attic W2-8 (`infra/loadtest/`, `docs/planning/load-baseline.md`),
adapted 2026-07-31 for the redesign stack on `anila-restart`.

> Numbers are **relative / regression** measurements against a **stubbed**
> embedder. They are not a production capacity commitment for 3000 users.

## What changed vs attic

| Attic (2026-07) | This package |
|---|---|
| Isolated `anila-loadtest` compose stack | Drives the running `anila-restart` stack (owner-approved); throwaway stub only |
| Real external gateway + Triton embed | Local OpenAI-compatible **stub** (`stub/`) with configurable `EMBED_DELAY_MS` |
| Primary probe: RAG chat SSE (`profile2`) | Primary probe: `POST /api/ingestion/collections/{id}/search` (`profile-search.js`) — exact `_embed_query` path of tonight's pool fix |
| Rewrote catalogue embed endpoint | Registers dedicated `loadtest-embed-stub` + `set-platform-embedding` (AUTO_REGISTER overwrites `nvidia/nv-embed-v2` on every CSP start) |
| Clearance grants in seed | Dropped — redesign owner path does not need them for admin uploads |

## Files

| File | Role |
|---|---|
| `profile-search.js` | Concurrent collection search (pool-fix path) |
| `control-stub-direct.js` | Control: hit stub, bypass ANILA |
| `lib/common.js` | Login / options |
| `stub/` | Throwaway embed (+ minimal chat) server |
| `setup-stub.sh` / `teardown-stub.sh` | Wire / unwind stub via API (no container recreate of the platform) |
| `seed-collection.sh` | Small synthetic corpus |
| `run-sweep.sh` | VU sweep + health sample |
| `pg-sample.sh` | `pg_stat_activity` JSONL during load |
| `measure-recovery.sh` | Wall-clock recovery after load stops |

## Quick start

```bash
export ANILA_PASSWORD='…'          # admin password; never commit
export ANILA_BASE_URL=https://127.0.0.1
export EMBED_DELAY_MS=500          # use 2000 to stress the old cliff shape

./infra/loadtest/setup-stub.sh
COLL=$(./infra/loadtest/seed-collection.sh 8)
ANILA_COLLECTION_ID=$COLL ./infra/loadtest/run-sweep.sh profile-search.js "1 2 4 8 16 24 32 48 64"

# Optional recovery clock
ANILA_COLLECTION_ID=$COLL ./infra/loadtest/measure-recovery.sh

./infra/loadtest/teardown-stub.sh
```

During a level: `pg-sample.sh` is started by `run-sweep.sh` automatically.
Stop if DB size approaches 512 MiB (hard guard in `pg-sample.sh`).

## Acceptable latency (this package's definition)

With stub delay \(D\) ms:

- **Acceptable p95** ≤ \(D + 500\) ms (platform overhead budget on top of stub)
- **Error onset** = first VU level with `search_fail_rate > 0.01` or `http_5xx > 0`

These thresholds measure the platform's own queueing/error behaviour, not
user-facing RAG quality with a real embedder.
