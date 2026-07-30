#!/usr/bin/env bash
# Tear down the throwaway stub and undo platform-embedding designation.
#   ANILA_PASSWORD=... ./infra/loadtest/teardown-stub.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BASE="${ANILA_BASE_URL:-https://127.0.0.1}"
CURL_INSECURE="${ANILA_CURL_INSECURE:--sk}"
STATE_FILE="${ROOT}/results/stub-state.json"
: "${ANILA_PASSWORD:?ANILA_PASSWORD must be exported}"

TOKEN="$(curl $CURL_INSECURE -sS -m 30 -H 'Content-Type: application/json' \
  -d "{\"username\":\"${ANILA_USER:-admin}\",\"password\":\"$ANILA_PASSWORD\"}" \
  "$BASE/api/auth/login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

STUB_NAME=anila-loadtest-stub-embed
MODEL_ID=""
PREV_PLATFORM_ID=""

if [[ -f "$STATE_FILE" ]]; then
  MODEL_ID="$(python3 -c 'import json; print(json.load(open("'"$STATE_FILE"'"))["model_id"])')"
  STUB_NAME="$(python3 -c 'import json; print(json.load(open("'"$STATE_FILE"'")).get("stub_name","anila-loadtest-stub-embed"))')"
  PREV_PLATFORM_ID="$(python3 -c 'import json; s=json.load(open("'"$STATE_FILE"'")); print(s["previous_platform_embedding_id"] or "")')"

  echo "unsetting platform embedding on model $MODEL_ID" >&2
  curl $CURL_INSECURE -sS -m 60 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    "$BASE/api/models/$MODEL_ID/unset-platform-embedding" >/dev/null || true

  if [[ -n "$PREV_PLATFORM_ID" ]]; then
    echo "restoring previous platform embedding id=$PREV_PLATFORM_ID" >&2
    curl $CURL_INSECURE -sS -m 60 -X POST \
      -H "Authorization: Bearer $TOKEN" \
      "$BASE/api/models/$PREV_PLATFORM_ID/set-platform-embedding" >/dev/null || true
  fi

  curl $CURL_INSECURE -sS -m 60 -X DELETE \
    -H "Authorization: Bearer $TOKEN" \
    "$BASE/api/models/$MODEL_ID" >/dev/null || true

  TH="$(curl $CURL_INSECURE -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/trusted-hosts")"
  TID="$(python3 -c 'import json,sys; rows=json.load(sys.stdin); name=sys.argv[1]
print(next((r["id"] for r in rows if r.get("host")==name), ""))' "$STUB_NAME" <<<"$TH")"
  if [[ -n "${TID:-}" ]]; then
    curl $CURL_INSECURE -sS -m 30 -X DELETE \
      -H "Authorization: Bearer $TOKEN" \
      "$BASE/api/trusted-hosts/$TID" >/dev/null || true
  fi
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$STUB_NAME"; then
  docker rm -f "$STUB_NAME" >/dev/null
  echo "removed $STUB_NAME" >&2
fi
echo "teardown done" >&2
