#!/usr/bin/env bash
# build-and-export-for-intranet.sh
# ============================================================================
# 把整套 ANILA stack (含 myCSPPlatform + ANILA UI + ANILALM + ingestion-worker
# + router + pptx-renderer + 3 個 base + 3 個 cold-service + 4 個 model) 全部
# build 完 → save 成 tar.gz,可以帶進無外網的內網環境 docker load。
#
# 用法 (在有外網的環境執行):
#   bash scripts/build-and-export-for-intranet.sh [OUTPUT_DIR]
#   OUTPUT_DIR 預設 /tmp/anila-images-export
#
# 輸出:
#   $OUTPUT_DIR/
#     ├── 01-anila-built.tar.gz      (csp / ui / lm / worker / router / pptx)
#     ├── 02-base.tar.gz             (postgres / redis / nginx)
#     ├── 03-cold.tar.gz             (codeserver / n8n / gitlab)
#     ├── 04-models.tar.gz           (4 個 model image,若 host 上沒就跳過)
#     ├── INTRANET-LOAD.sh           (內網端用的 import 腳本)
#     └── MANIFEST.txt               (image 清單 + 大小,給 IT 對 checksum)
#
# 內網端執行:
#   bash INTRANET-LOAD.sh
# ============================================================================
set -euo pipefail

OUTPUT_DIR="${1:-/tmp/anila-images-export}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"
mkdir -p "$OUTPUT_DIR"

echo "============================================================"
echo "ANILA — Build & Export for Intranet"
echo "  Repo:   $REPO_ROOT"
echo "  Output: $OUTPUT_DIR"
echo "============================================================"
echo

# ── Phase 1: build 6 個自家 image ──────────────────────────────────────────
echo "▶ [1/5] Building 6 self-built images via docker compose..."
docker compose build csp ingestion-worker router anilalm anila-ui pptx-renderer
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
# project name = anila-platform (見 docker-compose.yml ``name:`` 頂層欄位)。
echo "  • 01-anila-built.tar.gz"
docker save \
    anila-platform-csp \
    anila-platform-ingestion-worker \
    anila-platform-router \
    anila-platform-anilalm \
    anila-platform-anila-ui \
    anila-platform-pptx-renderer \
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

# ── Phase 4: model image (可選) ──────────────────────────────────────────
# Model images 由 models/docker-compose.yml 定義,通常事先 build 好放在
# host 上。如果本機 docker image 找得到就 save,找不到就略過 (印警告)。
echo "  • 04-models.tar.gz (optional — skipped if image missing)"
MODEL_IMAGES=(
    tensorrt-llm-hf:1.3.0rc10
    vllm-gemma4:latest
    tritonserver:25.04-nv-embed-v2
    embedding-proxy:migration
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
    echo "    (no model images on host — 04-models.tar.gz not created)"
fi
echo

# ── Phase 5: 寫 manifest + intranet import script ────────────────────────
echo "▶ [4/5] Writing MANIFEST.txt + INTRANET-LOAD.sh..."

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
    echo "── SHA256 checksum (IT 對檔用) ─────────────────────────"
    sha256sum "$OUTPUT_DIR"/*.tar.gz
} > "$OUTPUT_DIR/MANIFEST.txt"

cat > "$OUTPUT_DIR/INTRANET-LOAD.sh" <<'EOF'
#!/usr/bin/env bash
# INTRANET-LOAD.sh — 內網端 docker load 用。執行前確認:
#   1. docker 已裝且能跑 (docker info 不報錯)
#   2. 跟此檔同目錄底下放著 01~04-*.tar.gz
#   3. /home/aia/c1147259/ANILA repo 已 clone 到內網機器 (帶 .env 進去)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

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
