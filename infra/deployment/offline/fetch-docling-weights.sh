#!/usr/bin/env bash
# Fetch the Docling PDF pipeline artifacts for an air-gapped bundle.
#
# 2026-08-17 擁有者裁決之後的部署故事:docling 跑在**獨立的 GPU 主機**上,
# 不是平台映像裡。所以這支 script 在**有網路的機器**上跑,產出一個目錄;
# 那個目錄會被帶過氣隙、在 GPU 主機上掛載為 DOCLING_ARTIFACTS_DIR
# (compose 的 ${DOCLING_MODEL_HOST_DIR:-../../models/model/docling} 掛到
# /var/anila/docling-artifacts:ro)。平台的 csp / ingestion-worker 那側
# 完全不碰這些權重,只有 httpx client。
#
# 產出的目錄樹由 docling 自己釘形狀(layout model + tableformer + EasyOCR),
# 與 runtime DoclingConverter 的 artifacts_path 讀法一一對上,不靠本 script
# 再翻譯。每筆權重的來源與完整性記在 DOCLING-WEIGHTS-MANIFEST.txt
# (sha256),build-and-export-for-intranet.sh 打包時以它當「這目錄是 fetch
# 過的」憑證。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
FORCE=0

usage() {
    echo "用法: bash infra/deployment/offline/fetch-docling-weights.sh [--force] <destination>" >&2
    echo "  destination 必須在 repository 外；預設語言來自 DOCLING_OCR_LANGS（ch_tra,en）。" >&2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi
if [[ "${1:-}" == "--force" ]]; then
    FORCE=1
    shift
fi
if [[ $# -ne 1 ]]; then
    usage
    exit 2
fi

DEST_INPUT="$1"
DEST="$($PYTHON_BIN -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$DEST_INPUT")"
case "$DEST" in
    "$REPO_ROOT"|"$REPO_ROOT"/*)
        echo "✗ destination must be outside the repository: $DEST" >&2
        exit 2
        ;;
esac
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd -P)"

"$PYTHON_BIN" - "$DEST" "${DOCLING_OCR_LANGS:-ch_tra,en}" "$FORCE" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path


destination = Path(sys.argv[1])
languages = [item.strip() for item in sys.argv[2].split(",") if item.strip()]
force = bool(int(sys.argv[3]))

try:
    from docling.utils.model_downloader import download_models
except ImportError as exc:
    raise SystemExit(
        "✗ docling[easyocr] is required — this script runs on the networked "
        "machine that builds the GPU bundle, not in the platform image. "
        "Install docling[easyocr] before fetching weights."
    ) from exc

if not languages:
    raise SystemExit("✗ DOCLING_OCR_LANGS must contain at least one language")

# 下載集合對齊 runtime PdfPipelineOptions 的預設(runtime 只開 OCR + table
# structure;picture classifier / code-formula 都 OFF,不抓它們的權重):
#   with_layout=True      → layout model(Heron,可能含 engine 變體)
#   with_tableformer=True → tableformer(TableStructureModel)
#   with_easyocr=True     → EasyOCR(繁中/英 recognition + craft detection)
# EasyOCR 語言碼走 docling 的 _resolve_easyocr_recognition_models:
#   ch_tra → zh_tra_g1、en → english_g2。
download_models(
    output_dir=destination,
    force=force,
    progress=True,
    with_layout=True,
    with_tableformer=True,
    with_code_formula=False,
    with_picture_classifier=False,
    with_rapidocr=False,
    with_easyocr=True,
    easyocr_languages=languages,
)
PY

MANIFEST="$DEST/DOCLING-WEIGHTS-MANIFEST.txt"
rm -f -- "$MANIFEST"
(
    cd "$DEST"
    find . -type f ! -name "$(basename "$MANIFEST")" -print0 \
        | sort -z \
        | xargs -0 sha256sum > "$MANIFEST"
)

if [[ ! -s "$MANIFEST" ]]; then
    echo "✗ no Docling artifacts were fetched into $DEST" >&2
    exit 1
fi

echo "✓ Docling layout/table/EasyOCR artifacts fetched: $DEST"
echo "  mount this dir as DOCLING_ARTIFACTS_DIR on the GPU host (volume :ro)."
echo "  manifest: $MANIFEST ($(wc -l < "$MANIFEST") files)"
echo "  size: $(du -sh "$DEST" | cut -f1)"
