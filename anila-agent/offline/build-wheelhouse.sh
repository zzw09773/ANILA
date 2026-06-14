#!/usr/bin/env bash
# 建離線輪檔包：在「有網路」的機器上跑，把完整相依閉包下載成 wheel，
# 之後帶進 air-gapped 內網以 --no-index 安裝（驗證零網路可裝）。
#
#   bash offline/build-wheelhouse.sh
#   # 把整個 offline/wheelhouse/ 連同專案帶進內網，然後：
#   bash offline/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WHEELHOUSE="$HERE/wheelhouse"

EXTRAS="${1:-serving,pgvector,csp}"   # 預設不含 litellm；要時傳入 serving,pgvector,csp,litellm

mkdir -p "$WHEELHOUSE"
echo ">> 下載 anila-agent[$EXTRAS] 的完整相依閉包到 $WHEELHOUSE"
python3 -m pip download \
  --dest "$WHEELHOUSE" \
  "anila-agent[$EXTRAS] @ file://$ROOT"

echo ">> 把 anila-agent 本身打成 wheel（pip download 只抓相依，不含本套件）"
python3 -m pip wheel --no-deps --wheel-dir "$WHEELHOUSE" "$ROOT"

echo ">> 完成。wheel 數：$(ls -1 "$WHEELHOUSE"/*.whl 2>/dev/null | wc -l)"
echo ">> 帶進內網後執行 offline/install.sh（--no-index，全程不連網）。"
