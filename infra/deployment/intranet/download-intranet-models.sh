#!/usr/bin/env bash
# download-intranet-models.sh
# ============================================================================
# 內網模型權重下載 (2026-06-10 定稿後,2026-09-26 拿掉生圖權重)。
#
# ⚠ 通道 = Google Drive (無轉移碟),單檔上限 50G;本機只剩 ~1.3TB。
#   → 逐模型 pipeline:下載到本機暫存 → pack-chunks.sh 切 45GiB 塊
#     → rclone 上傳 Drive → 確認後刪本地 → 下一個。
#   清單順序 = 上傳優先序:H100 階段要用的在前,B200 期貨在後。
#   GEN_MANIFEST 的 sha256 仍建議開 (內網端最終落地驗檔用;chunk 層
#   的逐塊 hash 由 pack-chunks.sh 自己產)。
#
# 用法:
#   bash infra/deployment/intranet/download-intranet-models.sh /data/staging/hf
#
# 環境變數:
#   GEN_MANIFEST=1   下載完產生 WEIGHTS-CHECKSUMS.sha256 (2TB 約 30-60 分鐘,
#                    內網端 `sha256sum -c` 驗檔用 — 供應鏈防護,建議開)
#   HF_TOKEN         未設則用 ~/.cache/huggingface/token (gated repo 需要:
#                    meta-llama;403 = 還沒在 HF 網頁按同意)
#
# 已在本機的權重 (gemma-4-31B-it / assistant / gpt-oss-20b /
# NV-Embed-v2 / Scout-Instruct-FP8) 不在此清單重抓 — 直接從
# project/Huggingface 走 pack-chunks 上傳,不用過這支腳本。
# gemma-31B / Scout 已拍板用 instruct 版 (2026-06-10):gemma-4-31B-it
# 本機就有所以不列;Scout 列 Instruct bf16。
# ============================================================================
set -euo pipefail

DEST="${1:?用法: bash infra/deployment/intranet/download-intranet-models.sh <本機暫存目錄,如 /data/staging/hf>}"
mkdir -p "$DEST"

# repo|本地目錄名 (= infra/models/docker-compose.yml 掛載時用的目錄名)
# 順序 = 優先序:H100 階段運行清單在前 (小→大),B200 期貨在後,Maverick bf16 壓軸。
MODELS=(
  # ── H100 階段運行清單 (gemma-31B-it / NV-Embed 本機已有,不在此列) ────────
  "google/gemma-4-12B|gemma-4-12B"                                                     #  22.3 GiB
  "google/gemma-4-26B-A4B|gemma-4-26B-A4B"                                             #  48.1 GiB
  "openai/gpt-oss-120b|gpt-oss-120b"                                                   # 182.3 GiB
  # ── 工具/小模型 ──────────────────────────────────────────────────────────
  # Systran 這份已經是 CTranslate2 格式(faster-whisper 直接吃,不用自己轉檔)。
  # 語音輸入 (asr-decoder) 用;fp16 推論吃 ~4.7 GiB VRAM。
  "Systran/faster-whisper-large-v3|faster-whisper-large-v3"                            #   3.1 GiB
  "google/gemma-4-E4B|gemma-4-E4B"                                                     #  14.9 GiB (NVFP4 rehearsal 用)
  # ── B200 期貨 (instruct 定案:Scout 帶 Instruct,不帶 base) ───────────────
  "meta-llama/Llama-4-Scout-17B-16E-Instruct|Llama-4-Scout-17B-16E-Instruct"           # 202.4 GiB
  "mistralai/Mistral-Small-4-119B-2603|Mistral-Small-4-119B-2603"                      # 225.3 GiB
  "mistralai/Mistral-Medium-3.5-128B|Mistral-Medium-3.5-128B"                          # 248.9 GiB
  # ── Maverick 三版 (748G bf16 在 H100/B200 都跑不動原檔) ──────────────────
  # w4a16: H100 時期唯一能跑的版本 (INT4 weight-only,TP4)
  # FP8:   B200 換裝後的最佳版本 (官方出品,4×B200 ~768GB VRAM 放得下)
  # bf16:  萬用母本 (B200 後離線壓 NVFP4 的源頭),最大,壓軸下載。
  "RedHatAI/Llama-4-Maverick-17B-128E-Instruct-quantized.w4a16|Llama-4-Maverick-17B-128E-Instruct-w4a16"  # 201.1 GiB
  "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8|Llama-4-Maverick-17B-128E-Instruct-FP8"              # 388.2 GiB
  "meta-llama/Llama-4-Maverick-17B-128E-Instruct|Llama-4-Maverick-17B-128E-Instruct"   # 748.0 GiB (REMOVE_SOURCE=1 打包)
)
TOTAL_GIB=2286

command -v hf >/dev/null || { echo "✗ 找不到 hf CLI (pip install -U huggingface_hub[cli])"; exit 1; }
[ -n "${HF_TOKEN:-}" ] || [ -f "$HOME/.cache/huggingface/token" ] \
  || { echo "✗ 無 HF token — gated repo (meta-llama) 會 403"; exit 1; }

AVAIL_GIB=$(df -BG --output=avail "$DEST" | tail -1 | tr -dc '0-9')
echo "目的地: $DEST (可用 ${AVAIL_GIB} GiB / 全清單需 ~${TOTAL_GIB} GiB)"
if (( AVAIL_GIB < TOTAL_GIB )); then
  echo "ℹ 空間放不下完整清單 — 這是預期內 (本機只剩 ~1.3TB)。"
  echo "  逐模型 pipeline:下載 → pack-chunks → rclone 上傳 Drive → 刪本地 → 重跑本腳本續下一個。"
fi
echo

FAILED=()
for entry in "${MODELS[@]}"; do
  repo="${entry%%|*}"; dir="${entry#*|}"
  # 已存在且非空 = 先前下載完成 (hf download 自身也支援續傳,真要強制
  # 重新核對就刪掉該目錄重跑)
  if [ -d "$DEST/$dir" ] && [ -n "$(ls -A "$DEST/$dir" 2>/dev/null)" ]; then
    echo "↷ skip (已存在): $dir ($(du -sh "$DEST/$dir" | cut -f1))"
    continue
  fi
  echo "▶ $repo → $DEST/$dir"
  if hf download "$repo" --local-dir "$DEST/$dir"; then
    echo "✓ $dir ($(du -sh "$DEST/$dir" | cut -f1))"
  else
    echo "✗ 下載失敗: $repo (gated 未授權? 網路? 先記下,繼續下一個)"
    FAILED+=("$repo")
  fi
  echo
done

if [ "${GEN_MANIFEST:-0}" = "1" ]; then
  echo "▶ 產生 sha256 manifest (大,會跑一陣子)..."
  ( cd "$DEST" && find . -type f ! -name "WEIGHTS-CHECKSUMS.sha256" -print0 \
      | xargs -0 sha256sum > WEIGHTS-CHECKSUMS.sha256 )
  echo "✓ $DEST/WEIGHTS-CHECKSUMS.sha256 ($(wc -l < "$DEST/WEIGHTS-CHECKSUMS.sha256") files)"
  echo "  內網端驗檔: cd <DEST> && sha256sum -c WEIGHTS-CHECKSUMS.sha256 --quiet"
fi

echo "────────────────────────────────────────"
du -sh "$DEST"/*/ 2>/dev/null
if (( ${#FAILED[@]} > 0 )); then
  printf '⚠ 失敗 %d 個:\n' "${#FAILED[@]}"; printf '   %s\n' "${FAILED[@]}"; exit 1
fi
echo "✓ 全部完成。下一步:逐模型 pack-chunks.sh 切塊 → rclone 上傳 Google Drive。"
