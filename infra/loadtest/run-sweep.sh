#!/usr/bin/env bash
# W2-8 sweep driver — runs one k6 profile once per concurrency level.
#
# Each level is a separate constant-vus run rather than a single ramping run:
# a ramp smears the p95 of every level together, and the saturation point is
# precisely the level at which p95/error-rate stops scaling linearly. Discrete
# runs keep each level's numbers clean and independently reproducible.
#
#   ANILA_PASSWORD=... ./infra/loadtest/run-sweep.sh profile1-chat-sse.js "1 2 4 8 16 32"
#
# Results land as one JSON summary per level under infra/loadtest/results/
# (gitignored); the last line printed is a TSV table for the report.
set -euo pipefail

SCRIPT="${1:?usage: run-sweep.sh <profile.js> [levels] }"
LEVELS="${2:-1 2 4 8 16 32}"
DURATION="${ANILA_DURATION:-45s}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO_ROOT/infra/loadtest/results"
: "${ANILA_PASSWORD:?ANILA_PASSWORD must be exported}"

mkdir -p "$OUT"
TAG="$(basename "$SCRIPT" .js)"

for vus in $LEVELS; do
  echo "=== $TAG @ ${vus} VUs (${DURATION}) ===" >&2
  docker run --rm -i --network host \
    -e ANILA_BASE_URL="${ANILA_BASE_URL:-http://127.0.0.1:18000}" \
    -e ANILA_USER="${ANILA_USER:-admin}" \
    -e ANILA_PASSWORD="$ANILA_PASSWORD" \
    -e ANILA_CHAT_MODEL="${ANILA_CHAT_MODEL:-gemma26-nothink}" \
    -e ANILA_COLLECTION_ID="${ANILA_COLLECTION_ID:-}" \
    -e ANILA_EMBED_MODEL="${ANILA_EMBED_MODEL:-nv-embed-v2}" \
    -e ANILA_VUS="$vus" \
    -e ANILA_DURATION="$DURATION" \
    -e ANILA_UPLOAD_PACE="${ANILA_UPLOAD_PACE:-1.0}" \
    --user "$(id -u):$(id -g)" \
    -v "$REPO_ROOT/infra/loadtest:/scripts:ro" \
    -v "$OUT:/out" \
    grafana/k6 run --quiet --summary-export "/out/${TAG}-vu${vus}.json" \
      "/scripts/$SCRIPT" >&2 || echo "  (k6 exited non-zero at ${vus} VUs)" >&2

  # Cool down before the next level. Without this the next run's setup() can
  # time out while the stack is still draining, which reports as 0 iterations
  # and is indistinguishable in the results from "the platform collapsed".
  sleep "${ANILA_COOLDOWN:-20}"
done

# ── table ──────────────────────────────────────────────────────────────────
python3 - "$OUT" "$TAG" <<'PY'
import json, os, sys
out, tag = sys.argv[1], sys.argv[2]
def g(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur
rows = []
for fn in sorted(os.listdir(out)):
    if not (fn.startswith(tag + "-vu") and fn.endswith(".json")):
        continue
    vus = int(fn.rsplit("vu", 1)[1].split(".")[0])
    d = json.load(open(os.path.join(out, fn)))
    m = d.get("metrics", {})
    reqs = g(m, "http_reqs", "count", default=0)
    fail_rate = g(m, "http_req_failed", "value")
    if fail_rate is None:
        fail_rate = g(m, "anila_failed_streams", "value", default=0.0) or 0.0
    rows.append({
        "vus": vus,
        "iters": g(m, "iterations", "count", default=0),
        "reqs": reqs,
        "ttft_p95": g(m, "anila_ttft_ms", "p(95)"),
        "dur_p95": g(m, "anila_stream_complete_ms", "p(95)"),
        "upload_p95": g(m, "anila_upload_accept_ms", "p(95)"),
        "canary_p95": g(m, "anila_canary_ms", "p(95)"),
        "err": fail_rate,
    })
rows.sort(key=lambda r: r["vus"])
cols = ["vus", "iters", "reqs", "ttft_p95", "dur_p95", "upload_p95", "canary_p95", "err"]
print("\t".join(cols))
for r in rows:
    print("\t".join(
        "" if r[c] is None else (f"{r[c]:.1f}" if isinstance(r[c], float) else str(r[c]))
        for c in cols
    ))
PY
