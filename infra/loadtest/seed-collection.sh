#!/usr/bin/env bash
# Seed one ingested collection for the search profile.
# Reclaimed from attic seed-collection.sh; clearance grants dropped — redesign
# stack grants owner access without the old clearance evaluator path for
# admin/owner uploads. Embedding goes through whatever model registry currently
# points at (setup-stub.sh rewires nvidia/nv-embed-v2 to the throwaway stub).
#
#   ANILA_PASSWORD=... ./infra/loadtest/seed-collection.sh [doc_count]
# Prints collection id on the last stdout line.
set -euo pipefail

BASE="${ANILA_BASE_URL:-https://127.0.0.1}"
USER_NAME="${ANILA_USER:-admin}"
DOC_COUNT="${1:-10}"
CURL_INSECURE="${ANILA_CURL_INSECURE:--sk}"
: "${ANILA_PASSWORD:?ANILA_PASSWORD must be exported}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

api() { curl $CURL_INSECURE -sS -m 120 -H "Authorization: Bearer $TOKEN" "$@"; }

TOKEN="$(curl $CURL_INSECURE -sS -m 30 -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"$ANILA_PASSWORD\"}" \
  "$BASE/api/auth/login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
[ -n "$TOKEN" ] || { echo "login failed" >&2; exit 1; }

EMBED_MODEL="${ANILA_EMBED_MODEL:-nvidia/nv-embed-v2}"

COLL_JSON="$(api -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"loadtest-$(date +%s)\",\"description\":\"wt/load search corpus\",
       \"chunking_config\":{\"strategy\":\"fixed\",\"params\":{\"chunk_size\":600,\"chunk_overlap\":80}},
       \"embedding_model\":\"$EMBED_MODEL\"}" \
  "$BASE/api/ingestion/collections")"
COLL_ID="$(printf '%s' "$COLL_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "collection_id=$COLL_ID" >&2

for i in $(seq 1 "$DOC_COUNT"); do
  f="$WORK/doc-$i.md"
  {
    echo "# 平台運維手冊 第 $i 章"
    echo
    for s in $(seq 1 20); do
      echo "## 第 $i-$s 節 任務說明"
      echo "本節說明 ANILA 平台第 $i 章第 $s 節的運作方式。向量資料庫負責儲存文件切片的嵌入向量，"
      echo "檢索時以餘弦相似度找出最相關的段落。連線池大小與外送嵌入延遲決定並發搜尋的飽和點。"
      echo "章節編號 $i-$s 的設定值為 $((i * 100 + s))，逾時秒數為 $((30 + s))。"
      echo
    done
  } >"$f"
  api -X POST -F "file=@$f;type=text/markdown" \
    "$BASE/api/ingestion/collections/$COLL_ID/documents" >/dev/null
  echo "uploaded doc $i/$DOC_COUNT" >&2
done

echo "waiting for indexing (stop if DB grows past 512MiB — pg-sample guard)..." >&2
for _ in $(seq 1 120); do
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
  if [ "$(printf '%s' "$STATE" | grep -c "indexed=$DOC_COUNT")" = "1" ]; then
    echo "all documents indexed" >&2
    echo "$COLL_ID"
    exit 0
  fi
  sleep 3
done
echo "timed out waiting for indexing" >&2
exit 1
