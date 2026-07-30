#!/usr/bin/env bash
# Start the throwaway embed stub on anila-net and register a *dedicated*
# embedding model (loadtest-embed-stub) as the platform embedding.
#
# Do NOT rewrite nvidia/nv-embed-v2: AUTO_REGISTER_MODELS resets that row's
# endpoint_url on every CSP process start, which is exactly what invalidated
# the first sweep attempt tonight.
#
#   ANILA_PASSWORD=... ./infra/loadtest/setup-stub.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BASE="${ANILA_BASE_URL:-https://127.0.0.1}"
CURL_INSECURE="${ANILA_CURL_INSECURE:--sk}"
STUB_NAME="${ANILA_STUB_NAME:-anila-loadtest-stub-embed}"
STUB_NET="${ANILA_STUB_NET:-anila-net}"
DELAY_MS="${EMBED_DELAY_MS:-500}"
MODEL_NAME="${ANILA_LOADTEST_EMBED_MODEL:-loadtest-embed-stub}"
STATE_FILE="${ROOT}/results/stub-state.json"
: "${ANILA_PASSWORD:?ANILA_PASSWORD must be exported}"
mkdir -p "${ROOT}/results"
chmod 777 "${ROOT}/results" 2>/dev/null || true

TOKEN="$(curl $CURL_INSECURE -sS -m 30 -H 'Content-Type: application/json' \
  -d "{\"username\":\"${ANILA_USER:-admin}\",\"password\":\"$ANILA_PASSWORD\"}" \
  "$BASE/api/auth/login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

api() { curl $CURL_INSECURE -sS -m 60 -H "Authorization: Bearer $TOKEN" "$@"; }

if docker ps -a --format '{{.Names}}' | grep -qx "$STUB_NAME"; then
  docker rm -f "$STUB_NAME" >/dev/null
fi
docker build -t anila-loadtest-embed-stub:local "$ROOT/stub" >/dev/null
docker run -d --name "$STUB_NAME" --network "$STUB_NET" \
  -e EMBED_DELAY_MS="$DELAY_MS" -e EMBED_DIM=4096 -e PORT=8080 \
  -p 127.0.0.1:18088:8080 \
  anila-loadtest-embed-stub:local >/dev/null
echo "stub up: $STUB_NAME on $STUB_NET (host :18088, delay=${DELAY_MS}ms)" >&2

api -X POST -H 'Content-Type: application/json' \
  -d "{\"host\":\"$STUB_NAME\",\"note\":\"wt/load throwaway embed stub\"}" \
  "$BASE/api/trusted-hosts" >/dev/null || true

NEW_URL="http://${STUB_NAME}:8080"

# Find or create the dedicated loadtest embedding model.
MODEL_JSON="$(api "$BASE/api/models")"
MODEL_ID="$(printf '%s' "$MODEL_JSON" | python3 -c '
import json,sys
d=json.load(sys.stdin)
ms=d if isinstance(d,list) else d.get("items", d.get("models", []))
name="'"$MODEL_NAME"'"
for m in ms:
    if m.get("name")==name:
        print(m["id"]); break
')"

PREV_PLATFORM_ID="$(api "$BASE/api/models/platform-embedding" | python3 -c '
import json,sys
try:
  d=json.load(sys.stdin)
except Exception:
  d={}
# shape may be {model: {...}} or the model itself or 404 body
m=d.get("model") if isinstance(d,dict) and "model" in d else d
if isinstance(m,dict) and m.get("id"):
  print(m["id"])
' 2>/dev/null || true)"

if [[ -z "${MODEL_ID:-}" ]]; then
  echo "creating model $MODEL_NAME -> $NEW_URL" >&2
  MODEL_ID="$(api -X POST -H 'Content-Type: application/json' \
    -d "{\"name\":\"$MODEL_NAME\",\"display_name\":\"Loadtest embed stub\",\"model_type\":\"embedding\",\"endpoint_url\":\"$NEW_URL\",\"api_version\":\"v1\",\"description\":\"wt/load throwaway stub — safe to delete\",\"is_internal\":true}" \
    "$BASE/api/models" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
else
  echo "updating existing model id=$MODEL_ID -> $NEW_URL" >&2
  api -X PUT -H 'Content-Type: application/json' \
    -d "{\"endpoint_url\":\"$NEW_URL\",\"is_active\":true}" \
    "$BASE/api/models/$MODEL_ID" >/dev/null
fi

# Designate as platform embedding so search._embed_query uses it (not nv-embed).
SET_OUT="$(api -X POST "$BASE/api/models/$MODEL_ID/set-platform-embedding")"
printf '%s\n' "$SET_OUT" | python3 -c '
import json,sys
raw=sys.stdin.read()
try:
  d=json.loads(raw)
except Exception:
  print("set-platform-embedding raw:", raw[:800]); raise SystemExit(1)
if d.get("detail") and not d.get("id"):
  print("set-platform-embedding FAILED:", d); raise SystemExit(1)
print("platform embedding =>", d.get("name"), "id", d.get("id"), "native", d.get("embedding_native_dim") or d.get("measured_native_dim"))
' >&2

api -X POST "$BASE/api/models/$MODEL_ID/health-check" >/dev/null || true

python3 - "$STATE_FILE" "$MODEL_ID" "$MODEL_NAME" "$NEW_URL" "$STUB_NAME" "$DELAY_MS" "${PREV_PLATFORM_ID:-}" <<'PY'
import json,sys
path, mid, name, url, stub, delay, prev = sys.argv[1:]
json.dump({
  "model_id": int(mid),
  "model_name": name,
  "endpoint_url": url,
  "stub_name": stub,
  "embed_delay_ms": int(delay),
  "previous_platform_embedding_id": int(prev) if prev.strip() else None,
  "strategy": "dedicated-model-as-platform-embedding",
}, open(path,"w"), indent=2)
print(path)
PY

# Smoke via search-path model: /v1/embeddings with the stub model name.
SMOKE="$(api -X POST -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL_NAME\",\"input\":\"stub-smoke\"}" \
  "$BASE/v1/embeddings" || true)"
printf '%s' "$SMOKE" | python3 -c '
import json,sys
try:
  d=json.load(sys.stdin)
except Exception as e:
  print("embed smoke FAILED to parse:", e); raise SystemExit(1)
data=d.get("data") or []
if not data or not data[0].get("embedding"):
  print("embed smoke FAILED:", str(d)[:500]); raise SystemExit(1)
print("embed smoke ok dim=", len(data[0]["embedding"]))
' >&2

echo "state: $STATE_FILE" >&2
