#!/usr/bin/env bash
# download-intranet-toolkit.sh
# ============================================================================
# 內網「離線量化 + 新模型 serving」工具鏈打包。通道只開一次 — 這包是讓
# 內網日後 (尤其 B200 換裝後) 能自給自足的關鍵:
#
#   1. wheelhouse/        llm-compressor 全依賴 wheel — 內網從 bf16 母本
#                         離線壓 NVFP4 / FP8 / w4a16 用 (B200 到了壓 NVFP4)
#   2. calib-datasets/    量化校準資料集 (llm-compressor 範例慣用,離線必備)
#   3. 06-vllm-openai.tar.gz  通用 vLLM serving image — Llama-4 / Mistral /
#                         gpt-oss-120b / gemma-4 全系列都靠它跑;現有的
#                         model image 都是綁定單一模型的客製品,蓋不到新清單
#   4. 09-age-*.tar.gz    age / age-keygen 靜態 binary — **生產備份的硬相依**
#                         (production_backup.py 把 DB/state 直接串流進 age)。
#                         內網 apt 不通,沒帶進去就等於「以為有 DR,其實沒有」
#
# 用法:
#   bash infra/deployment/intranet/download-intranet-toolkit.sh /data/staging/toolkit
#   (產出後 pack-chunks.sh 切塊 → rclone 上傳 Google Drive;>45G 檔走檔案模式)
#
# 環境變數:
#   VLLM_IMAGE     default vllm/vllm-openai:v0.22.1-cu129-ubuntu2404
#                  (2026-06-05 穩定版;cu129 = Hopper + Blackwell 雙代支援;
#                   0.22 起沒有裸 tag,一律帶後綴)
#   TRITON_IMAGE   default nvcr.io/nvidia/tritonserver:26.05-py3
#                  (Triton 2.69.0 stable, 2026-06-02)
#   TRTLLM_IMAGE   default nvcr.io/nvidia/tensorrt-llm/release:1.2.1
#                  (TRT-LLM 穩定版 2026-04-20;1.3.0 仍在 RC — 現役
#                   tensorrt-llm-hf:1.3.0rc10 是 RC,能跑照舊,新部署用這個)
#   SKIP_NV=1      跳過兩個 nvcr.io image (nvcr 拉不動時先出 vLLM 包)
#   AGE_VERSION    default v1.3.1 (改版號時 AGE_SHA256 必須一起換,否則會被擋)
#   AGE_SHA256     對應 age-<ver>-linux-amd64.tar.gz 的 sha256
#
# nvcr.io 公開 image 通常可匿名拉;403/401 → docker login nvcr.io
# (帳號 $oauthtoken / 密碼 = NGC API key)。
# ============================================================================
set -euo pipefail

DEST="${1:?用法: bash infra/deployment/intranet/download-intranet-toolkit.sh <目的地目錄>}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.22.1-cu129-ubuntu2404}"
TRITON_IMAGE="${TRITON_IMAGE:-nvcr.io/nvidia/tritonserver:26.05-py3}"
TRTLLM_IMAGE="${TRTLLM_IMAGE:-nvcr.io/nvidia/tensorrt-llm/release:1.2.1}"
AGE_VERSION="${AGE_VERSION:-v1.3.1}"
AGE_SHA256="${AGE_SHA256:-bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377}"
AGE_ARCHIVE="09-age-${AGE_VERSION}-linux-amd64.tar.gz"
mkdir -p "$DEST"/{wheelhouse,calib-datasets}

echo "▶ [1/4] pip wheelhouse (llm-compressor 全依賴,內網 pip install --no-index 用)"
# --no-deps 不行 — 內網沒 PyPI,整棵依賴樹都要齊。同平台 (linux x86_64) +
# 同 python 大版本即可;intranet 端裝法:
#   pip install --no-index --find-links=wheelhouse/ llmcompressor datasets
python3 -m pip download llmcompressor datasets accelerate \
  -d "$DEST/wheelhouse" 2>&1 | tail -3
echo "  ✓ $(ls "$DEST/wheelhouse" | wc -l) wheels, $(du -sh "$DEST/wheelhouse" | cut -f1)"

echo "▶ [2/4] 校準資料集 (量化 calibration 用,離線環境抓不到 HF datasets)"
for ds in "garage-bAInd/Open-Platypus" "HuggingFaceH4/ultrachat_200k"; do
  dir="$DEST/calib-datasets/$(basename "$ds")"
  if [ -d "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "  ↷ skip (已存在): $ds"
  else
    hf download "$ds" --repo-type dataset --local-dir "$dir"
    echo "  ✓ $ds ($(du -sh "$dir" | cut -f1))"
  fi
done

echo "▶ [3/4] age / age-keygen 靜態 binary (生產備份的硬相依,內網 apt 不通)"
FAILED_AGE=0
if [ -f "$DEST/$AGE_ARCHIVE" ]; then
  echo "  ↷ skip (已存在): $AGE_ARCHIVE"
else
  age_url="https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-amd64.tar.gz"
  if curl -fsSL --retry 3 -o "$DEST/$AGE_ARCHIVE.part" "$age_url"; then
    got="$(sha256sum "$DEST/$AGE_ARCHIVE.part" | cut -d' ' -f1)"
    if [ "$got" = "$AGE_SHA256" ]; then
      mv -f "$DEST/$AGE_ARCHIVE.part" "$DEST/$AGE_ARCHIVE"
      echo "  ✓ $AGE_ARCHIVE ($(du -sh "$DEST/$AGE_ARCHIVE" | cut -f1)), sha256 相符"
    else
      rm -f "$DEST/$AGE_ARCHIVE.part"
      echo "  ✗ age sha256 不符 — 期望 $AGE_SHA256,實得 $got (不放行未驗證的備份工具)"
      FAILED_AGE=1
    fi
  else
    rm -f "$DEST/$AGE_ARCHIVE.part"
    echo "  ✗ age 下載失敗: $age_url"
    FAILED_AGE=1
  fi
fi

echo "▶ [4/4] 推論伺服器 image (vLLM / Triton / TRT-LLM)"
save_image() {
  local img="$1" out="$2"
  if [ -f "$DEST/$out" ]; then echo "  ↷ skip (已存在): $out"; return 0; fi
  if docker pull "$img"; then
    docker save "$img" | gzip > "$DEST/$out"
    echo "  ✓ $out ($(du -sh "$DEST/$out" | cut -f1))"
  else
    echo "  ✗ pull 失敗: $img (nvcr 需要 docker login nvcr.io?)"; return 1
  fi
}
FAILED_IMG=0
save_image "$VLLM_IMAGE" "06-vllm-openai.tar.gz" || FAILED_IMG=1
if [ "${SKIP_NV:-0}" != "1" ]; then
  save_image "$TRITON_IMAGE" "07-tritonserver.tar.gz" || FAILED_IMG=1
  save_image "$TRTLLM_IMAGE" "08-tensorrt-llm.tar.gz" || FAILED_IMG=1
fi

( cd "$DEST" && shopt -s nullglob && sha256sum *.tar.gz > TOOLKIT-CHECKSUMS.sha256 )
grep -q " $AGE_ARCHIVE\$" "$DEST/TOOLKIT-CHECKSUMS.sha256" \
  || { echo "  ✗ manifest 缺 $AGE_ARCHIVE — 沒有 age 的離線包不能當備份可用的憑據"; FAILED_AGE=1; }
echo
echo "✓ 完成: $DEST"
du -sh "$DEST"/*
echo
echo "內網端:"
echo "  image:  for t in 0[1-8]-*.tar.gz; do gunzip -c \"\$t\" | docker load; done"
echo "  age:    tar -xzf $AGE_ARCHIVE && sudo install -m 0755 age/age age/age-keygen /usr/local/bin/"
echo "          (裝完 'anila-ops.sh health' 的『age 可用』該轉綠;沒有 age = 備份 100% 失敗)"
echo "  量化環境 (在 vLLM 容器或任何同版 python):"
echo "    pip install --no-index --find-links=wheelhouse/ llmcompressor datasets"
exit $(( FAILED_IMG || FAILED_AGE ))
