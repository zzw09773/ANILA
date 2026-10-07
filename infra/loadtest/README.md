# ANILA load test (k6)

Concurrent collection search against the running `anila` compose project. The embedder is a local stub. The numbers are relative to that stub. They are not a capacity figure for production.

`setup-stub.sh` registers `loadtest-embed-stub` and calls `set-platform-embedding`. `AUTO_REGISTER` overwrites `nvidia/nv-embed-v2` on every CSP start. The seed script does not insert clearance grants.

Primary probe: `POST /api/ingestion/collections/{id}/search` (`profile-search.js`). The stub is an OpenAI-compatible server under `stub/`; `EMBED_DELAY_MS` sets its delay.

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
