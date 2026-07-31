#!/usr/bin/env bash
# Run one k6 profile across a concurrency sweep.
# Reclaimed from attic W2-8; drives grafana/k6 via Docker (--network host).
#
#   ANILA_PASSWORD=... ANILA_COLLECTION_ID=... \
#     ./infra/loadtest/run-sweep.sh profile-search.js "1 2 4 8 16 32 48"
set -euo pipefail

SCRIPT="${1:?profile js}"
LEVELS="${2:-1 2 4 8 16 32}"
DURATION="${ANILA_DURATION:-30s}"
COOLDOWN="${ANILA_COOLDOWN:-20}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
OUT_DIR="${ROOT}/results"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%dT%H%M%S)"
SUMMARY="${OUT_DIR}/sweep-${STAMP}.tsv"
echo -e "vus\tp50_ms\tp95_ms\tp99_ms\tfail_rate\thttp_5xx\titerations\thealth_ms\tpg_peak_total\tpg_peak_idle_in_xact" >"$SUMMARY"

: "${ANILA_PASSWORD:?ANILA_PASSWORD must be set}"
BASE="${ANILA_BASE_URL:-https://127.0.0.1}"

health_ms() {
  local start end
  start=$(date +%s%3N)
  code=$(curl -sk -o /dev/null -w '%{http_code}' -m 5 "${BASE}/health" || echo 000)
  end=$(date +%s%3N)
  if [[ "$code" != "200" ]]; then
    echo "health_fail:${code}"
  else
    echo $((end - start))
  fi
}

peak_from_pg() {
  local file="$1" field="$2"
  [[ -f "$file" ]] || { echo "na"; return; }
  python3 - "$file" "$field" <<'PY'
import json,sys
path,field=sys.argv[1],sys.argv[2]
peak=None
for line in open(path):
    try: o=json.loads(line)
    except Exception: continue
    v=o.get(field)
    if isinstance(v,(int,float)):
        peak = v if peak is None else max(peak,v)
print("na" if peak is None else int(peak) if float(peak)==int(peak) else peak)
PY
}

SCRIPT_PATH="$ROOT/$SCRIPT"
[[ -f "$SCRIPT_PATH" ]] || SCRIPT_PATH="$SCRIPT"
[[ -f "$SCRIPT_PATH" ]] || { echo "script not found: $SCRIPT" >&2; exit 1; }

for vu in $LEVELS; do
  echo "=== VU=$vu duration=$DURATION ===" >&2
  label="vu${vu}-${STAMP}"
  pg_file="${OUT_DIR}/pg-${label}.jsonl"
  # Sample a bit longer than the k6 run so we catch drain.
  sample_secs=$(python3 - <<PY
d="$DURATION"
print(int(d.rstrip("s")) + 10)
PY
)
  "$ROOT/pg-sample.sh" "$label" "$sample_secs" 2 &
  PG_PID=$!

  set +e
  docker run --rm --network host \
    -v "$ROOT:/scripts:ro" \
    -v "$OUT_DIR:/results:rw" \
    -e ANILA_BASE_URL="${BASE}" \
    -e ANILA_PASSWORD \
    -e ANILA_USER="${ANILA_USER:-admin}" \
    -e ANILA_COLLECTION_ID="${ANILA_COLLECTION_ID:-}" \
    -e ANILA_CHAT_MODEL="${ANILA_CHAT_MODEL:-}" \
    -e ANILA_VUS="$vu" \
    -e ANILA_DURATION="$DURATION" \
    -e ANILA_INSECURE_TLS=1 \
    grafana/k6 run --summary-export="/results/k6-${label}.json" \
      "/scripts/$(basename "$SCRIPT_PATH")" \
      | tee "${OUT_DIR}/k6-${label}.out"
  k6_rc=$?
  set -e

  wait "$PG_PID" || true
  h="$(health_ms)"
  # Parse the text summary lines we emit from the profile.
  p50=$(grep -E '^search_p50_ms=' "${OUT_DIR}/k6-${label}.out" | tail -1 | cut -d= -f2)
  p95=$(grep -E '^search_p95_ms=' "${OUT_DIR}/k6-${label}.out" | tail -1 | cut -d= -f2)
  p99=$(grep -E '^search_p99_ms=' "${OUT_DIR}/k6-${label}.out" | tail -1 | cut -d= -f2)
  fail=$(grep -E '^search_fail_rate=' "${OUT_DIR}/k6-${label}.out" | tail -1 | cut -d= -f2)
  x5=$(grep -E '^http_5xx=' "${OUT_DIR}/k6-${label}.out" | tail -1 | cut -d= -f2)
  iters=$(grep -E '^iterations=' "${OUT_DIR}/k6-${label}.out" | tail -1 | cut -d= -f2)
  peak_total=$(peak_from_pg "$pg_file" total)
  peak_iix=$(peak_from_pg "$pg_file" idle_in_xact)
  echo -e "${vu}\t${p50}\t${p95}\t${p99}\t${fail}\t${x5}\t${iters}\t${h}\t${peak_total}\t${peak_iix}" | tee -a "$SUMMARY"
  echo "(k6_rc=${k6_rc}) cooling ${COOLDOWN}s..." >&2
  sleep "$COOLDOWN"
done

echo "SUMMARY $SUMMARY" >&2
cat "$SUMMARY"
