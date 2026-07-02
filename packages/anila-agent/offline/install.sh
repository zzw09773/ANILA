#!/usr/bin/env bash
# 在 air-gapped 內網以離線輪檔包安裝（全程 --no-index，不連網）。
# 前置：先在有網路的機器跑 offline/build-wheelhouse.sh，把 offline/wheelhouse/ 帶進來。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WHEELHOUSE="$HERE/wheelhouse"
VENV="${VENV:-$ROOT/.venv}"
EXTRAS="${1:-serving}"

if [ ! -d "$WHEELHOUSE" ] || [ -z "$(ls -A "$WHEELHOUSE" 2>/dev/null)" ]; then
  echo "!! $WHEELHOUSE 為空。請先在有網路的機器跑 offline/build-wheelhouse.sh。" >&2
  exit 1
fi

python3 -m venv "$VENV"
"$VENV/bin/pip" install --no-index --find-links "$WHEELHOUSE" --upgrade pip || true
echo ">> 以 --no-index 安裝 anila-agent[$EXTRAS]（從預建 wheel，不連網、不重 build）"
"$VENV/bin/pip" install --no-index --find-links "$WHEELHOUSE" "anila-agent[$EXTRAS]"
echo ">> 完成。啟動：$VENV/bin/anila"
