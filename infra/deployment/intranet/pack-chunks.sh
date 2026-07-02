#!/usr/bin/env bash
# pack-chunks.sh — 把目錄或大檔切成 ≤45GiB chunk (50G 轉入通道用)
# ============================================================================
# 用法:
#   bash infra/deployment/intranet/pack-chunks.sh <來源目錄或檔案> <輸出目錄> [chunk大小]
#
# 範例:
#   bash infra/deployment/intranet/pack-chunks.sh models/model/gemma-4-31B  /staging  # 權重目錄
#   bash infra/deployment/intranet/pack-chunks.sh export/04-models.tar.gz   /staging  # 既有大檔
#
# 產出 (以 gemma-4-31B 為例):
#   gemma-4-31B.tar.part-000, -001, ...   (目錄 → tar 串流切塊,無壓縮)
#   gemma-4-31B.manifest.sha256           (逐 chunk hash,邊切邊算,單次 IO)
#   檔案來源則為 <檔名>.part-000 ... (raw split,不再包 tar)
#
# chunk 預設 45G (GiB):「50G」若指十進位 GB,48GiB=51.5GB 會超限;
# 45GiB≈48.3GB 兩種解讀都安全。
#
# 內網端重組/解壓: bash infra/deployment/intranet/unpack-chunks.sh (串流,不需雙倍磁碟)
# 失敗域 = 單一 chunk:哪包壞了看 manifest 重傳那包就好。
#
# REMOVE_SOURCE=1 (目錄模式限定):tar 邊打包邊刪來源檔 (--remove-files),
# 磁碟峰值從「來源+chunks」降到「max(來源,chunks)+一個chunk」。
# 給 Maverick (748G,本機 1.3TB 塞不下兩份) 這種等級用;刪了就沒有重來,
# 確定 chunks+manifest 落地前不要動它們。
# ============================================================================
set -euo pipefail

SRC="${1:?用法: pack-chunks.sh <來源目錄或檔案> <輸出目錄> [chunk大小,預設45G]}"
OUT="${2:?需要輸出目錄}"
CHUNK="${3:-45G}"

mkdir -p "$OUT"
NAME="$(basename "$SRC")"

# split --filter:每個 chunk 經 tee 落地的同時算 sha256 — 單次 IO,
# 不用切完再整批重讀一次 (2TB 級資料省下數小時)。
write_chunks() {  # stdin → chunks
  local prefix="$1" manifest="$2"
  : > "$manifest"
  MANIFEST="$manifest" split -b "$CHUNK" -d -a 3 \
    --filter='tee "$FILE" | sha256sum | { read h _; echo "$h  $(basename "$FILE")" >> "$MANIFEST"; }' \
    - "$prefix"
}

if [[ -d "$SRC" ]]; then
  echo "▶ 目錄模式: $SRC ($(du -sh "$SRC" | cut -f1)) → tar 串流切塊"
  # -C 父目錄、打包目錄名:內網端解出來就是 <name>/ 結構。
  # 不加 -h:權重若是 symlink (dev 機) 會打包成連結 — 要打包真實資料請
  # 對真實路徑跑 (e.g. /home/aia/c1147259/project/Huggingface/<name>)。
  if [[ -L "$SRC" ]]; then
    echo "⚠ $SRC 是 symlink — 改對真實路徑打包,避免 tar 進去的只是連結:"
    echo "   bash $0 $(readlink -f "$SRC") $OUT $CHUNK"
    exit 1
  fi
  TAR_EXTRA=()
  if [[ "${REMOVE_SOURCE:-0}" == "1" ]]; then
    echo "⚠ REMOVE_SOURCE=1:邊打包邊刪來源 (磁碟峰值減半,但不可重來)"
    TAR_EXTRA=(--remove-files)
  fi
  tar -cf - "${TAR_EXTRA[@]}" -C "$(dirname "$SRC")" "$NAME" \
    | write_chunks "$OUT/$NAME.tar.part-" "$OUT/$NAME.manifest.sha256"
elif [[ -f "$SRC" ]]; then
  echo "▶ 檔案模式: $SRC ($(du -sh "$SRC" | cut -f1)) → raw 切塊"
  write_chunks "$OUT/$NAME.part-" "$OUT/$NAME.manifest.sha256" < "$SRC"
else
  echo "✗ 來源不存在: $SRC"; exit 1
fi

echo "✓ 完成:"
ls -lh "$OUT/$NAME"*part-* | awk '{print "   " $9 "  " $5}'
echo "   manifest: $OUT/$NAME.manifest.sha256 ($(wc -l < "$OUT/$NAME.manifest.sha256") chunks)"
