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
CORE="$(cd "$ROOT/../anila-core" && pwd)"
WHEELHOUSE="$HERE/wheelhouse"

EXTRAS="${1:-serving,pgvector,csp}"   # 預設不含 litellm；要時傳入 serving,pgvector,csp,litellm

mkdir -p "$WHEELHOUSE"

# pip wheel / download 會「追加」wheel，不會取代同名舊檔；殘留的高版本
# anila_core-*.whl / anila_agent-*.whl 會在後續 --no-index --find-links
# 時以最高版本勝出。先清掉這兩套件的舊 wheel，再重新打。
rm -f "$WHEELHOUSE"/anila_core-*.whl "$WHEELHOUSE"/anila_agent-*.whl

# anila-core 未上 PyPI：必須先 path-pin 打進 wheelhouse。
# --no-deps 只產本套件 wheel；其傳遞相依由下一步 pip download 一併抓齊。
echo ">> 先把 in-repo anila-core 以 path-pin 打進 $WHEELHOUSE"
python3 -m pip wheel --no-deps --wheel-dir "$WHEELHOUSE" "$CORE"

# --find-links 只是「多一個」候選來源，預設 index 仍會被查詢，且 pip 按
# 最高版本選（與來源無關）。若 PyPI 出現更高版 anila-core，裸名解析會
# 靜默換成公開包。用 PEP 508 direct-reference constraint 把 anila-core
# 釘死在本機 path，download 步才不會被 index 上的高版本蓋掉。
echo "anila-core @ file://$CORE" > "$WHEELHOUSE/constraints.txt"

echo ">> 下載 anila-agent[$EXTRAS] 的完整相依閉包到 $WHEELHOUSE"
echo ">> （-c constraints.txt 以 PEP 508 file:// 釘死 anila-core，不採 index 高版本）"
python3 -m pip download \
  --dest "$WHEELHOUSE" \
  --find-links "$WHEELHOUSE" \
  -c "$WHEELHOUSE/constraints.txt" \
  "anila-agent[$EXTRAS] @ file://$ROOT"

echo ">> 把 anila-agent 本身打成 wheel（pip download 只抓相依，不含本套件）"
python3 -m pip wheel --no-deps --wheel-dir "$WHEELHOUSE" "$ROOT"

echo ">> 完成。wheel 數：$(ls -1 "$WHEELHOUSE"/*.whl 2>/dev/null | wc -l)"
echo ">> 帶進內網後執行 offline/install.sh（--no-index，全程不連網）。"
