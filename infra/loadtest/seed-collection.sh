#!/usr/bin/env bash
# Seed one ingested collection for the RAG profile (W2-8, profile 2).
#
# Creates a collection, uploads N synthetic markdown documents, and waits until
# every one of them reaches status=completed. Prints the collection id on the
# last line so the caller can feed it to profile2 as ANILA_COLLECTION_ID.
#
#   ANILA_PASSWORD=... ./infra/loadtest/seed-collection.sh [doc_count]
#
# No credentials live in this file — ANILA_PASSWORD must come from the
# environment.
set -euo pipefail

BASE="${ANILA_BASE_URL:-http://127.0.0.1:18000}"
USER_NAME="${ANILA_USER:-admin}"
DOC_COUNT="${1:-5}"
: "${ANILA_PASSWORD:?ANILA_PASSWORD must be exported (never hard-code it)}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

api() { curl -sS -m 120 -H "Authorization: Bearer $TOKEN" "$@"; }

# ── auth ───────────────────────────────────────────────────────────────────
TOKEN="$(curl -sS -m 30 -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"$ANILA_PASSWORD\"}" \
  "$BASE/api/auth/login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
[ -n "$TOKEN" ] || { echo "login failed" >&2; exit 1; }

# ── SSRF allow-list for the embedding shim ─────────────────────────────────
# Added for the post-fix rerun. The outbound SSRF guard rejects single-label
# hostnames, so the model health probe never reaches `http://embed-proxy:8000`
# and parks nv-embed-v2 as `unhealthy`; the proxy circuit breaker then refuses
# every embedding call with 503 model_unhealthy and each document fails with
# [E_EMBED_MODEL_DOWN]. Registering the compose service name as a trusted host
# is the supported way past that check. Idempotent: a repeat POST returns 409.
api -X POST -H 'Content-Type: application/json' \
  -d '{"host":"embed-proxy","note":"W2-8 load-test embedding shim (compose service name)"}' \
  "$BASE/api/trusted-hosts" > /dev/null || true
# Re-probe so the breaker clears before the first upload.
EMBED_MODEL_ID="$(api "$BASE/api/models" | python3 -c '
import json,sys
d=json.load(sys.stdin); ms=d if isinstance(d,list) else d.get("items", d.get("models", []))
w=[m["id"] for m in ms if m.get("name")=="'"${ANILA_EMBED_MODEL:-nv-embed-v2}"'"]
print(w[0] if w else "")')"
[ -n "$EMBED_MODEL_ID" ] && api -X POST "$BASE/api/models/$EMBED_MODEL_ID/health-check" > /dev/null || true
echo "embedding endpoint trusted + re-probed" >&2

# ── collection ─────────────────────────────────────────────────────────────
COLL_JSON="$(api -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"loadtest-$(date +%s)\",\"description\":\"W2-8 load baseline corpus\",
       \"chunking_config\":{\"strategy\":\"fixed\",\"params\":{\"chunk_size\":600,\"chunk_overlap\":80}},
       \"embedding_model\":\"${ANILA_EMBED_MODEL:-nv-embed-v2}\"}" \
  "$BASE/api/ingestion/collections")"
COLL_ID="$(printf '%s' "$COLL_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "collection_id=$COLL_ID" >&2

# ── clearance ──────────────────────────────────────────────────────────────
# The ingestion worker re-evaluates canonical clearance at every sensitive
# boundary (evaluator.py::_require_eval_data_clearance) and the queued job row
# is explicitly NOT an authority token. A brand-new admin holds no grant, so
# without these two calls every document fails with
# "eval run data clearance is no longer valid" while the API still returns 202.
USER_ID="$(api "$BASE/api/auth/me" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
NOW="$(date -u -d '-5 minutes' +%Y-%m-%dT%H:%M:%SZ)"
END="$(date -u -d '+1 day' +%Y-%m-%dT%H:%M:%SZ)"

GRANT_ID="$(api -X POST -H 'Content-Type: application/json' \
  -d "{\"subject_user_id\":$USER_ID,\"max_classification_level\":\"無機密\",
       \"valid_from\":\"$NOW\",\"expires_at\":\"$END\",
       \"basis_ticket\":\"W2-8-loadtest\"}" \
  "$BASE/api/clearance/grants" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "clearance_grant_id=$GRANT_ID" >&2

api -X POST -H 'Content-Type: application/json' \
  -d '{"membership_granted":true,"need_to_know":true,"basis_ticket":"W2-8-loadtest"}' \
  "$BASE/api/clearance/grants/$GRANT_ID/collections/$COLL_ID" > /dev/null
echo "collection access granted" >&2

# ── documents ──────────────────────────────────────────────────────────────
# Each document is distinct so retrieval has real ranking work to do; identical
# files would be rejected by the (collection_id, sha256) unique constraint.
for i in $(seq 1 "$DOC_COUNT"); do
  f="$WORK/doc-$i.md"
  {
    echo "# 平台運維手冊 第 $i 章"
    echo
    for s in $(seq 1 40); do
      echo "## 第 $i-$s 節 服務說明"
      echo "本節說明 ANILA 平台第 $i 章第 $s 節的運作方式。向量資料庫負責儲存文件切片的嵌入向量，"
      echo "檢索時以餘弦相似度找出最相關的段落。快取層可降低重複查詢的延遲，"
      echo "而稽核紀錄則保存每一次存取的來源與時間，供事後追溯使用。"
      echo "章節編號 $i-$s 的設定值為 $((i * 100 + s))，逾時秒數為 $((30 + s))。"
      echo
    done
  } > "$f"
  api -X POST -F "file=@$f;type=text/markdown" \
      "$BASE/api/ingestion/collections/$COLL_ID/documents" > /dev/null
  echo "uploaded doc $i/$DOC_COUNT" >&2
done

# ── wait for indexing ──────────────────────────────────────────────────────
echo "waiting for indexing..." >&2
for _ in $(seq 1 180); do
  STATE="$(api "$BASE/api/ingestion/collections/$COLL_ID/documents" | python3 -c '
import json,sys
rows=json.load(sys.stdin)
from collections import Counter
c=Counter(r["status"] for r in rows)
print(" ".join(f"{k}={v}" for k,v in sorted(c.items())), "total", len(rows))
')"
  echo "  $STATE" >&2
  case "$STATE" in
    *failed*) echo "indexing failed" >&2; exit 1 ;;
  esac
  # Terminal success state is 'indexed' (processing_stage 'complete'),
  # not 'completed'.
  if [ "$(printf '%s' "$STATE" | grep -c "indexed=$DOC_COUNT")" = "1" ]; then
    echo "all documents indexed" >&2
    echo "$COLL_ID"
    exit 0
  fi
  sleep 5
done
echo "timed out waiting for indexing" >&2
exit 1
