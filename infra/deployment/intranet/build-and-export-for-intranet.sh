#!/usr/bin/env bash
# build-and-export-for-intranet.sh
# ============================================================================
# 把整套 ANILA stack (含 csp + anila-shell + anilalm + ingestion-worker
# + router + pptx-renderer + 3 個 base + 3 個 cold-service + 4 個 model) 全部
# build 完 → save 成 tar.gz,可以帶進無外網的內網環境 docker load。
#
# 用法 (在有外網的環境執行):
#   bash infra/deployment/intranet/build-and-export-for-intranet.sh [OUTPUT_DIR]
#   OUTPUT_DIR 預設 /tmp/anila-images-export
#
# 環境變數:
#   WITH_MODELS=1   把 model image 一起打包進 04-models.tar.gz。
#                    預設 OFF — image 數十 GB,確定內網要本機跑模型才開。
#   WITH_WEIGHTS=1  把 HF 權重打包成 05-weights-<name>.tar (無壓縮 —
#                    safetensors 壓不動,gzip 數百 GB 純耗時)。
#                    內網無下載通道,權重只能從這裡帶。
#   WEIGHTS_LIST    要打包的權重目錄名 (空白分隔),預設內網需要的最小集:
#                    "FLUX.2-dev gemma-4-31B-it gemma-4-31B-it-assistant"
#                    (gemma-4-31B-it-assistant 是 MTP 投機解碼的 draft model,
#                    跑 gemma4 必帶;gpt-oss/NV-Embed 預設不帶 — aiagent2
#                    gateway 已服務,要重複部署再自行加進清單)
#   ANILA_HF_DIR    權重來源目錄 (default /home/aia/c1147259/project/Huggingface)
#
# 輸出:
#   $OUTPUT_DIR/
#     ├── 01-anila-built.tar.gz      (csp / ui / lm / worker / router / pptx / studio)
#     ├── 02-base.tar.gz             (postgres / redis / nginx)
#     ├── 03-cold.tar.gz             (codeserver / n8n / gitlab)
#     ├── 04-models.tar.gz           (僅當 WITH_MODELS=1)
#     ├── INTRANET-LOAD.sh           (內網端用的 import 腳本)
#     └── MANIFEST.txt               (image 清單 + 大小,給 IT 對 checksum)
#
# 內網端執行:
#   bash INTRANET-LOAD.sh
# ============================================================================
set -euo pipefail

OUTPUT_DIR="${1:-/tmp/anila-images-export}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

cd "$REPO_ROOT"
mkdir -p "$OUTPUT_DIR"

echo "============================================================"
echo "ANILA — Build & Export for Intranet"
echo "  Repo:   $REPO_ROOT"
echo "  Output: $OUTPUT_DIR"
echo "============================================================"
echo

# ── Phase 1: build 7 個自家 image ──────────────────────────────────────────
echo "▶ [1/5] Building 7 self-built images via docker compose..."
docker compose build csp ingestion-worker router anilalm anila-ui pptx-renderer anila-studio
echo "✓ Built."
echo

# ── Phase 2: pull base + cold-service image (外網) ────────────────────────
echo "▶ [2/5] Pulling 6 upstream images (base + cold-service)..."
for img in \
    pgvector/pgvector:pg16 \
    redis:7-alpine \
    nginx:alpine \
    codercom/code-server:latest \
    n8nio/n8n:1.98.2 \
    gitlab/gitlab-ce:16.10.10-ce.0
do
    echo "  - $img"
    docker pull "$img"
done
echo "✓ Pulled."
echo

# ── Phase 3: save tar.gz ──────────────────────────────────────────────────
echo "▶ [3/5] Saving images to compressed tar.gz..."

# 自家 build 出來的 image 名稱由 compose project name + service name 決定。
# project name = anila-platform (見 infra/compose/platform.yml ``name:`` 頂層欄位)。
echo "  • 01-anila-built.tar.gz"
docker save \
    anila-platform-csp \
    anila-platform-ingestion-worker \
    anila-platform-router \
    anila-platform-anilalm \
    anila-platform-anila-ui \
    anila-platform-pptx-renderer \
    anila-platform-anila-studio \
  | gzip > "$OUTPUT_DIR/01-anila-built.tar.gz"

echo "  • 02-base.tar.gz"
docker save \
    pgvector/pgvector:pg16 \
    redis:7-alpine \
    nginx:alpine \
  | gzip > "$OUTPUT_DIR/02-base.tar.gz"

echo "  • 03-cold.tar.gz"
docker save \
    codercom/code-server:latest \
    n8nio/n8n:1.98.2 \
    gitlab/gitlab-ce:16.10.10-ce.0 \
  | gzip > "$OUTPUT_DIR/03-cold.tar.gz"

# ── Phase 4: model image (預設跳過,WITH_MODELS=1 啟用) ──────────────────
# Model image 動輒 45+ GB (Triton + TensorRT-LLM + vLLM weights),通常走
# 別的管道 (USB / 內部資料閘道) 進內網。預設不打包,需要時:
#   WITH_MODELS=1 bash infra/deployment/intranet/build-and-export-for-intranet.sh
if [ "${WITH_MODELS:-0}" = "1" ]; then
    echo "  • 04-models.tar.gz (WITH_MODELS=1)"
    # 內網拓撲備註 (2026-06):gpt-oss/nv-embed 已由 aiagent2 gateway 服務,
    # image 通常不必帶;優先帶 FLUX (獨家繪圖) + gemma4 (VLM captions)。
    # 權重 (~295GB) 不打包 — 內網下載通道直接抓 HuggingFace。
    MODEL_IMAGES=(
        tensorrt-llm-hf:1.3.0rc10
        vllm-gemma4:latest
        tritonserver:25.04-nv-embed-v2
        embedding-proxy:migration
        flux2-dev:bf16
        anila-flux-agent:latest
    )
    EXISTING_MODELS=()
    for img in "${MODEL_IMAGES[@]}"; do
        if docker image inspect "$img" >/dev/null 2>&1; then
            EXISTING_MODELS+=("$img")
        else
            echo "    ⚠  missing on host, skip: $img"
        fi
    done

    if [ ${#EXISTING_MODELS[@]} -gt 0 ]; then
        docker save "${EXISTING_MODELS[@]}" | gzip > "$OUTPUT_DIR/04-models.tar.gz"
        echo "    ✓ Saved ${#EXISTING_MODELS[@]} model image(s)"
    else
        echo "    (no model images found — 04-models.tar.gz not created)"
    fi
else
    echo "  • 04-models.tar.gz — skipped (WITH_MODELS=0,model 走別的管道進內網)"
fi
echo

# ── Phase 4b: HF 權重 (預設跳過,WITH_WEIGHTS=1 啟用) ─────────────────────
# 內網沒有對外下載通道 — 權重只能在外網抓好再轉進去。無壓縮 tar:
# safetensors 已是高熵格式,gzip 換不到體積只換到小時級的 CPU 時間。
if [ "${WITH_WEIGHTS:-0}" = "1" ]; then
    HF_DIR="${ANILA_HF_DIR:-/home/aia/c1147259/project/Huggingface}"
    WEIGHTS_LIST="${WEIGHTS_LIST:-FLUX.2-dev gemma-4-31B-it gemma-4-31B-it-assistant}"
    echo "  • 05-weights-*.tar (WITH_WEIGHTS=1,來源 $HF_DIR)"
    for w in $WEIGHTS_LIST; do
        if [ -d "$HF_DIR/$w" ]; then
            echo "    - $w ($(du -sh "$HF_DIR/$w" | cut -f1))"
            tar -cf "$OUTPUT_DIR/05-weights-${w}.tar" -C "$HF_DIR" "$w"
        else
            echo "    ⚠  missing, skip: $HF_DIR/$w"
        fi
    done
else
    echo "  • 05-weights-*.tar — skipped (WITH_WEIGHTS=0)"
fi
echo

# ── Phase 5: 寫 manifest + intranet import script ────────────────────────
echo "▶ [4/5] Writing MANIFEST.txt + INTRANET-LOAD.sh..."

# CHECKSUMS.sha256:純機器可讀格式 (相對路徑),供 INTRANET-LOAD.sh 內網端
# `sha256sum -c` 自動驗檔用。MANIFEST.txt 內也保留一份人類可讀版,給 IT 對檔。
( cd "$OUTPUT_DIR" && shopt -s nullglob && sha256sum *.tar.gz *.tar > CHECKSUMS.sha256 )

{
    echo "ANILA Platform — Intranet Image Bundle"
    echo "Built at: $(date -Iseconds)"
    echo "Built by: $(whoami)@$(hostname)"
    echo "Repo:     $REPO_ROOT"
    echo "Branch:   $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'n/a')"
    echo "Commit:   $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'n/a')"
    echo
    echo "── Image files ────────────────────────────────────────"
    ls -lh "$OUTPUT_DIR"/*.tar.gz 2>/dev/null
    echo
    echo "── SHA256 checksum (IT 對檔用,機器驗檔請用 CHECKSUMS.sha256) ──"
    cat "$OUTPUT_DIR/CHECKSUMS.sha256"
} > "$OUTPUT_DIR/MANIFEST.txt"

cat > "$OUTPUT_DIR/INTRANET-LOAD.sh" <<'EOF'
#!/usr/bin/env bash
# INTRANET-LOAD.sh — 內網端 docker load 用。執行前確認:
#   1. docker 已裝且能跑 (docker info 不報錯)
#   2. 跟此檔同目錄底下放著 01~04-*.tar.gz + CHECKSUMS.sha256
#   3. /home/aia/c1147259/ANILA repo 已 clone 到內網機器 (帶 .env 進去)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# ── SHA256 完整性檢查 (供應鏈防護) ──────────────────────────────────────
# tar.gz 從外網機器經 USB / 內部閘道送進內網,任何中途竄改都可能塞後門進
# image。`sha256sum -c` 比對 build 時計算的 hash,不通過就拒絕 load。
if [ -f CHECKSUMS.sha256 ]; then
    echo "── Verifying SHA256 checksums ──"
    if ! sha256sum -c CHECKSUMS.sha256; then
        echo
        echo "✗ Checksum mismatch — tar.gz 與 build 時的 hash 不符,拒絕 load。"
        echo "  可能原因:傳輸中損毀、或檔案被竄改。請重新從外網取得 bundle。"
        exit 1
    fi
    echo "✓ All checksums verified."
    echo
else
    echo "⚠ CHECKSUMS.sha256 不存在 — 略過完整性檢查 (不建議在 prod 用)"
    echo
fi

echo "── Loading ANILA images into local docker ──"
for tar in 01-anila-built.tar.gz 02-base.tar.gz 03-cold.tar.gz 04-models.tar.gz; do
    if [ -f "$tar" ]; then
        echo "▶ $tar"
        gunzip -c "$tar" | docker load
    else
        echo "  (missing $tar — skipped)"
    fi
done
echo

# ── HF 權重 (05-weights-*.tar,WITH_WEIGHTS=1 打包時才有) ────────────────
# 解到 ANILA_HF_DIR — 慣例是 <repo>/models/model (infra/models/
# docker-compose.yml 的權重掛載預設)。先 export ANILA_HF_DIR=<repo>/models/model
# 再跑本腳本;放別處就之後在 .env 設同一個值。
HF_DIR="${ANILA_HF_DIR:-/home/aia/c1147259/project/Huggingface}"
shopt -s nullglob
WEIGHT_TARS=(05-weights-*.tar)
if [ ${#WEIGHT_TARS[@]} -gt 0 ]; then
    echo "── Extracting model weights → $HF_DIR ──"
    mkdir -p "$HF_DIR"
    for tar in "${WEIGHT_TARS[@]}"; do
        echo "▶ $tar"
        tar -xf "$tar" -C "$HF_DIR"
    done
    echo
fi
echo "── Verifying ──"
docker images | grep -E "anila-platform|pgvector|redis|nginx|code-server|n8n|gitlab|tensorrt-llm-hf|vllm-gemma4|tritonserver|embedding-proxy" || true
echo
echo "✓ Load complete. 後續步驟:"
echo "   cd <repo-root>"
echo "   docker network create anila-models-net  # 若還沒建"
echo "   docker compose up -d --no-build"
EOF
chmod +x "$OUTPUT_DIR/INTRANET-LOAD.sh"

# ── 完成 ─────────────────────────────────────────────────────────────────
echo "✓ Done."
echo
echo "▶ [5/5] Summary"
du -sh "$OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
echo
echo "============================================================"
echo "下一步:把 $OUTPUT_DIR/ 整個 (含 INTRANET-LOAD.sh) 帶進內網,"
echo "然後在內網執行 bash INTRANET-LOAD.sh 就會把所有 image load 進去。"
echo "============================================================"
