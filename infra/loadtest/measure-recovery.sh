#!/usr/bin/env bash
# After a sweep: hammer /health + one search until both are "normal", report
# wall-clock recovery. Old platform needed >8 minutes.
#
#   ANILA_PASSWORD=... ANILA_COLLECTION_ID=... ./infra/loadtest/measure-recovery.sh
set -euo pipefail

BASE="${ANILA_BASE_URL:-https://127.0.0.1}"
CURL_INSECURE="${ANILA_CURL_INSECURE:--sk}"
: "${ANILA_PASSWORD:?}"
: "${ANILA_COLLECTION_ID:?}"
TIMEOUT_S="${RECOVERY_TIMEOUT_S:-600}"
# "Normal" = health 200 in <200ms AND search 200 in < (stub_delay*3 + 1000)ms.
STUB_DELAY_MS="${EMBED_DELAY_MS:-500}"
SEARCH_BUDGET_MS=$((STUB_DELAY_MS * 3 + 1000))

TOKEN="$(curl $CURL_INSECURE -sS -m 30 -H 'Content-Type: application/json' \
  -d "{\"username\":\"${ANILA_USER:-admin}\",\"password\":\"$ANILA_PASSWORD\"}" \
  "$BASE/api/auth/login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

start=$(date +%s%3N)
deadline=$((start + TIMEOUT_S * 1000))
ok_streak=0
while :; do
  now=$(date +%s%3N)
  if (( now > deadline )); then
    echo "RECOVERY_TIMEOUT after ${TIMEOUT_S}s" >&2
    exit 1
  fi
  h_start=$(date +%s%3N)
  h_code=$(curl $CURL_INSECURE -o /dev/null -w '%{http_code}' -m 5 "$BASE/health" || echo 000)
  h_ms=$(( $(date +%s%3N) - h_start ))

  s_start=$(date +%s%3N)
  s_code=$(curl $CURL_INSECURE -o /tmp/anila-recovery-search.json -w '%{http_code}' -m 60 \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"query":"recovery probe","top_k":3,"min_score":0}' \
    "$BASE/api/ingestion/collections/${ANILA_COLLECTION_ID}/search" || echo 000)
  s_ms=$(( $(date +%s%3N) - s_start ))

  elapsed=$(( now - start ))
  echo "t+${elapsed}ms health=${h_code}/${h_ms}ms search=${s_code}/${s_ms}ms" >&2

  if [[ "$h_code" == "200" && "$h_ms" -lt 200 && "$s_code" == "200" && "$s_ms" -lt "$SEARCH_BUDGET_MS" ]]; then
    ok_streak=$((ok_streak + 1))
  else
    ok_streak=0
  fi
  if (( ok_streak >= 3 )); then
    echo "RECOVERED_MS=$elapsed"
    exit 0
  fi
  sleep 1
done
