#!/usr/bin/env bash
# unpack-chunks.sh — 內網端:驗 chunk hash → 串流重組/解壓 (pack-chunks 的對端)
# ============================================================================
# 用法:
#   bash scripts/unpack-chunks.sh <manifest檔> <輸出目錄>
#
# 範例:
#   bash scripts/unpack-chunks.sh /transfer/gemma-4-31B.manifest.sha256 models/model
#     → 驗每個 chunk 的 sha256 → cat | tar -x 直接解出 models/model/gemma-4-31B/
#   bash scripts/unpack-chunks.sh /transfer/04-models.tar.gz.manifest.sha256 ./restore
#     → 驗 hash → cat 重組回 ./restore/04-models.tar.gz (raw 檔案模式)
#
# 串流設計:tar 模式下 chunk 直接 cat 進 tar -x,**不需要**先重組出完整
# tar 檔 — 磁碟只要放得下解壓結果,不用雙倍空間。
# 驗證失敗會列出壞的 chunk 檔名 → 只重傳那幾包。
# ============================================================================
set -euo pipefail

MANIFEST="${1:?用法: unpack-chunks.sh <manifest檔> <輸出目錄>}"
DEST="${2:?需要輸出目錄}"

SRC_DIR="$(cd "$(dirname "$MANIFEST")" && pwd)"
mkdir -p "$DEST"

echo "── 驗 chunk 完整性 ──"
BAD=0
while read -r hash fname; do
  [[ -z "$hash" || "$hash" == \#* ]] && continue
  if [[ ! -f "$SRC_DIR/$fname" ]]; then
    echo "✗ 缺檔: $fname"; BAD=1; continue
  fi
  actual="$(sha256sum "$SRC_DIR/$fname" | cut -d' ' -f1)"
  if [[ "$actual" == "$hash" ]]; then
    echo "✓ $fname"
  else
    echo "✗ HASH 不符: $fname (重傳這包)"; BAD=1
  fi
done < "$MANIFEST"
(( BAD == 0 )) || { echo; echo "✗ 有 chunk 缺漏/損毀 — 重傳上列檔案後重跑"; exit 1; }

# manifest 檔名規律: <name>.tar.part-* = tar 串流模式;<name>.part-* = raw 檔
first_chunk="$(awk 'NR==1{print $2}' "$MANIFEST")"
base="${first_chunk%.part-*}"

echo
if [[ "$base" == *.tar ]]; then
  echo "── 串流解壓 → $DEST/ ──"
  cat "$SRC_DIR/$base".part-* | tar -xf - -C "$DEST"
  echo "✓ 解壓完成: $DEST/${base%.tar}/"
else
  echo "── 重組檔案 → $DEST/$base ──"
  cat "$SRC_DIR/$base".part-* > "$DEST/$base"
  echo "✓ 重組完成: $DEST/$base"
fi
