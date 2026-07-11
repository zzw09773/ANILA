#!/usr/bin/env bash
# build-and-export-for-intranet.sh
# ============================================================================
# 把整套 ANILA formal platform stack (13 個預設 service image + 1 個
# developer-tools profile image) build/pull 完 → save 成 tar.gz,可以帶進
# 無外網的內網環境 docker load。正式 image 集合的 SSOT 是同目錄的
# platform-image-inventory.tsv；Compose 新增服務卻沒同步 inventory 會 fail-fast。
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
#   ANILA_HF_DIR    權重來源目錄 (default $HOME/project/Huggingface)
#
# 輸出:
#   $OUTPUT_DIR/
#     ├── 01-anila-built.tar.gz      (8 個自建 image,含 flux2-dev-agent)
#     ├── 02-base.tar.gz             (postgres / redis / nginx)
#     ├── 03-cold.tar.gz             (n8n / gitlab + opt-in codeserver)
#     ├── 04-models.tar.gz           (僅當 WITH_MODELS=1)
#     ├── PLATFORM-IMAGE-INVENTORY.tsv (正式/optional profile image 清單)
#     ├── PLATFORM-IMAGE-LOCK.tsv      (每個 platform tag 的輸出 image ID)
#     ├── MODEL-IMAGE-INVENTORY.tsv   (optional model compose service/image 清單)
#     ├── MODEL-IMAGE-STATUS.tsv      (optional model bundle 實際收錄狀態)
#     ├── INTRANET-LOAD.sh           (內網端用的 import 腳本)
#     └── MANIFEST.txt               (image 清單 + 大小,給 IT 對 checksum)
#
# 內網端執行:
#   bash INTRANET-LOAD.sh
# ============================================================================
set -euo pipefail

OUTPUT_DIR="${1:-/tmp/anila-images-export}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
INVENTORY_FILE="$REPO_ROOT/infra/deployment/intranet/platform-image-inventory.tsv"
MODEL_INVENTORY_FILE="$REPO_ROOT/infra/deployment/intranet/model-image-inventory.tsv"
INVENTORY_CHECKER="$REPO_ROOT/infra/deployment/intranet/check_airgap_inventory.py"
IMAGE_LOCK_VERIFIER="$REPO_ROOT/infra/deployment/scripts/verify-compose-image-lock.py"
PLATFORM_LOCK_FILE="$OUTPUT_DIR/PLATFORM-IMAGE-LOCK.tsv"

cd "$REPO_ROOT"
mkdir -p "$OUTPUT_DIR"

for compose_variable in COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME \
  COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PATH_SEPARATOR; do
    [ -z "${!compose_variable:-}" ] || {
        echo "✗ exporter 不接受 ambient $compose_variable；請 unset 後重跑" >&2
        exit 1
    }
done

# 不把前一次的 04-models / weights 混進這次 manifest。要求 fresh output
# 比默默 rm 掉 operator 的大型 bundle 安全,也讓 STATUS 與實際檔案一一對應。
shopt -s nullglob
OUTPUT_CANDIDATES=(
    "$OUTPUT_DIR"/01-anila-built.tar.gz
    "$OUTPUT_DIR"/02-base.tar.gz
    "$OUTPUT_DIR"/03-cold.tar.gz
    "$OUTPUT_DIR"/04-models.tar.gz
    "$OUTPUT_DIR"/05-weights-*.tar
    "$OUTPUT_DIR"/CHECKSUMS.sha256
    "$OUTPUT_DIR"/MANIFEST.txt
    "$OUTPUT_DIR"/INTRANET-LOAD.sh
    "$OUTPUT_DIR"/PLATFORM-IMAGE-INVENTORY.tsv
    "$OUTPUT_DIR"/PLATFORM-IMAGE-LOCK.tsv
    "$OUTPUT_DIR"/MODEL-IMAGE-INVENTORY.tsv
    "$OUTPUT_DIR"/MODEL-IMAGE-STATUS.tsv
)
EXISTING_OUTPUT=()
for artifact in "${OUTPUT_CANDIDATES[@]}"; do
    [ -e "$artifact" ] && EXISTING_OUTPUT+=("$artifact")
done
if [ ${#EXISTING_OUTPUT[@]} -gt 0 ]; then
    echo "✗ OUTPUT_DIR 含前次 bundle 產物,為避免 stale image 混包請改用空目錄:" >&2
    printf '  - %s\n' "${EXISTING_OUTPUT[@]}" >&2
    exit 1
fi

# Formal Compose intentionally has no mutable tag fallback.  The connected
# exporter is the sole trusted build mode: resolve the same inventory tags
# explicitly for build/pull, then freeze their resulting content IDs below.
while IFS=$'\t' read -r image_variable image_value; do
    [ -n "$image_variable" ] || continue
    printf -v "$image_variable" '%s' "$image_value"
    export "$image_variable"
done < <(python3 "$IMAGE_LOCK_VERIFIER" emit-build-env)

# 先做 closure check,避免 build 數小時後才發現某個正式 service 沒打包。
python3 "$INVENTORY_CHECKER" --root "$REPO_ROOT" --inventory "$INVENTORY_FILE"
cp "$INVENTORY_FILE" "$OUTPUT_DIR/PLATFORM-IMAGE-INVENTORY.tsv"
cp "$MODEL_INVENTORY_FILE" "$OUTPUT_DIR/MODEL-IMAGE-INVENTORY.tsv"

inventory_images_for_bundle() {
    awk -F '\t' -v bundle="$1" '
        $0 !~ /^#/ && NF == 5 && $3 == bundle { print $2 }
    ' "$INVENTORY_FILE"
}

echo "============================================================"
echo "ANILA — Build & Export for Intranet"
echo "  Repo:   $REPO_ROOT"
echo "  Output: $OUTPUT_DIR"
echo "============================================================"
echo

# ── Phase 1: build inventory 內的自家 image ───────────────────────────────
mapfile -t BUILT_SERVICES < <(awk -F '\t' '
    $0 !~ /^#/ && NF == 5 && $5 == "built" { print $1 }
' "$INVENTORY_FILE")
echo "▶ [1/5] Building ${#BUILT_SERVICES[@]} self-built images via docker compose..."
docker compose build "${BUILT_SERVICES[@]}"
echo "✓ Built."
echo

# ── Phase 2: pull inventory 內的 upstream image (外網) ────────────────────
mapfile -t UPSTREAM_IMAGES < <(awk -F '\t' '
    $0 !~ /^#/ && NF == 5 && $5 == "upstream" { print $2 }
' "$INVENTORY_FILE")
echo "▶ [2/5] Pulling ${#UPSTREAM_IMAGES[@]} upstream images..."
for img in "${UPSTREAM_IMAGES[@]}"; do
    echo "  - $img"
    docker pull "$img"
done
echo "✓ Pulled."
echo

# Freeze the exact local image IDs that the following docker-save archives
# contain. The loader compares these IDs after load, so a stale same-name tag
# already present on the intranet host cannot satisfy closure.
printf '# service\timage\timage_id\tbundle\tactivation\n' > "$PLATFORM_LOCK_FILE"
while IFS=$'\t' read -r service image bundle activation source; do
    [ -n "${service:-}" ] || continue
    [[ "$service" == \#* ]] && continue
    image_id="$(docker image inspect --format '{{.Id}}' "$image")" \
        || { echo "✗ Cannot lock missing image: $service -> $image" >&2; exit 1; }
    [[ "$image_id" == sha256:* ]] \
        || { echo "✗ Unexpected image ID for $service: $image_id" >&2; exit 1; }
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$service" "$image" "$image_id" "$bundle" "$activation" \
        >> "$PLATFORM_LOCK_FILE"
done < "$INVENTORY_FILE"

# ── Phase 3: save tar.gz ──────────────────────────────────────────────────
echo "▶ [3/5] Saving images to compressed tar.gz..."

# image 名稱與 bundle 歸屬全部從 PLATFORM inventory 讀,不在腳本另留第二份清單。
echo "  • 01-anila-built.tar.gz"
mapfile -t BUNDLE_IMAGES < <(inventory_images_for_bundle 01-anila-built.tar.gz)
docker save "${BUNDLE_IMAGES[@]}" | gzip > "$OUTPUT_DIR/01-anila-built.tar.gz"

echo "  • 02-base.tar.gz"
mapfile -t BUNDLE_IMAGES < <(inventory_images_for_bundle 02-base.tar.gz)
docker save "${BUNDLE_IMAGES[@]}" | gzip > "$OUTPUT_DIR/02-base.tar.gz"

echo "  • 03-cold.tar.gz"
mapfile -t BUNDLE_IMAGES < <(inventory_images_for_bundle 03-cold.tar.gz)
docker save "${BUNDLE_IMAGES[@]}" | gzip > "$OUTPUT_DIR/03-cold.tar.gz"

# ── Phase 4: model image (預設跳過,WITH_MODELS=1 啟用) ──────────────────
# Model image 動輒 45+ GB (Triton + TensorRT-LLM + vLLM weights),通常走
# 別的管道 (USB / 內部資料閘道) 進內網。預設不打包,需要時:
#   WITH_MODELS=1 bash infra/deployment/intranet/build-and-export-for-intranet.sh
MODEL_STATUS_FILE="$OUTPUT_DIR/MODEL-IMAGE-STATUS.tsv"
mapfile -t MODEL_IMAGES < <(awk -F '\t' '
    $0 !~ /^#/ && NF == 3 && !seen[$2]++ { print $2 }
' "$MODEL_INVENTORY_FILE")
model_activation_for_image() {
    awk -F '\t' -v image="$1" '
        $0 !~ /^#/ && NF == 3 && $2 == image {
            if ($3 == "default") has_default = 1
            else if (profile == "") profile = $3
        }
        END { if (has_default) print "default"; else print profile }
    ' "$MODEL_INVENTORY_FILE"
}
printf '# image\tactivation\tstatus\tnote\n' > "$MODEL_STATUS_FILE"
if [ "${WITH_MODELS:-0}" = "1" ]; then
    echo "  • 04-models.tar.gz (WITH_MODELS=1)"
    # 內網拓撲備註 (2026-06):gpt-oss/nv-embed 已由 aiagent2 gateway 服務,
    # image 通常不必帶;優先帶 FLUX (獨家繪圖) + gemma4 (VLM captions)。
    # 權重 (~295GB) 不打包 — 內網下載通道直接抓 HuggingFace。
    EXISTING_MODELS=()
    for img in "${MODEL_IMAGES[@]}"; do
        activation="$(model_activation_for_image "$img")"
        if docker image inspect "$img" >/dev/null 2>&1; then
            EXISTING_MODELS+=("$img")
            printf '%s\t%s\tincluded\tWITH_MODELS=1\n' "$img" "$activation" >> "$MODEL_STATUS_FILE"
        else
            echo "    ⚠  missing on host, skip: $img"
            printf '%s\t%s\tmissing\tnot present on export host\n' "$img" "$activation" >> "$MODEL_STATUS_FILE"
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
    for img in "${MODEL_IMAGES[@]}"; do
        activation="$(model_activation_for_image "$img")"
        printf '%s\t%s\tskipped\tWITH_MODELS=0\n' "$img" "$activation" >> "$MODEL_STATUS_FILE"
    done
fi
echo

# ── Phase 4b: HF 權重 (預設跳過,WITH_WEIGHTS=1 啟用) ─────────────────────
# 內網沒有對外下載通道 — 權重只能在外網抓好再轉進去。無壓縮 tar:
# safetensors 已是高熵格式,gzip 換不到體積只換到小時級的 CPU 時間。
if [ "${WITH_WEIGHTS:-0}" = "1" ]; then
    HF_DIR="${ANILA_HF_DIR:-$HOME/project/Huggingface}"
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
( cd "$OUTPUT_DIR" && shopt -s nullglob && \
  sha256sum *.tar.gz *.tar PLATFORM-IMAGE-INVENTORY.tsv MODEL-IMAGE-INVENTORY.tsv \
    PLATFORM-IMAGE-LOCK.tsv MODEL-IMAGE-STATUS.tsv \
    > CHECKSUMS.sha256 )

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
    echo "── Formal platform image inventory ───────────────────"
    cat "$OUTPUT_DIR/PLATFORM-IMAGE-INVENTORY.tsv"
    echo
    echo "── Formal platform image ID lock ─────────────────────"
    cat "$OUTPUT_DIR/PLATFORM-IMAGE-LOCK.tsv"
    echo
    echo "── Optional model image status ────────────────────────"
    cat "$OUTPUT_DIR/MODEL-IMAGE-INVENTORY.tsv"
    echo
    cat "$MODEL_STATUS_FILE"
    echo
    echo "── SHA256 checksum (IT 對檔用,機器驗檔請用 CHECKSUMS.sha256) ──"
    cat "$OUTPUT_DIR/CHECKSUMS.sha256"
} > "$OUTPUT_DIR/MANIFEST.txt"

cat > "$OUTPUT_DIR/INTRANET-LOAD.sh" <<'EOF'
#!/usr/bin/env bash
# INTRANET-LOAD.sh — 內網端 docker load 用。執行前確認:
#   1. docker 已裝且能跑 (docker info 不報錯)
#   2. 同目錄有 01~03 tar.gz、IMAGE-INVENTORY、IMAGE-LOCK、STATUS、CHECKSUMS；
#      04-models / 05-weights 則依 bundle 選項存在
#   3. $HOME/ANILA repo 已 clone 到內網機器 (帶 .env 進去)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# ── SHA256 完整性檢查 (fail-closed transport integrity) ─────────────────
# tar.gz 從外網機器經 USB / 內部閘道送進內網,任何中途竄改都可能塞後門進
# image。`sha256sum -c` 比對 build 時計算的 hash,不通過就拒絕 load。
REQUIRED_BUNDLE_FILES=(
    CHECKSUMS.sha256
    PLATFORM-IMAGE-INVENTORY.tsv
    PLATFORM-IMAGE-LOCK.tsv
    MODEL-IMAGE-INVENTORY.tsv
    MODEL-IMAGE-STATUS.tsv
    01-anila-built.tar.gz
    02-base.tar.gz
    03-cold.tar.gz
)
for required_file in "${REQUIRED_BUNDLE_FILES[@]}"; do
    [ -s "$required_file" ] || {
        echo "✗ Required bundle file missing/empty: $required_file" >&2
        exit 1
    }
done

echo "── Verifying SHA256 checksums ──"
if ! sha256sum -c CHECKSUMS.sha256; then
    echo
    echo "✗ Checksum mismatch/missing entry — 拒絕 load。"
    echo "  可能原因:傳輸中損毀、bundle 不完整、或檔案被竄改。請重新取得 bundle。"
    exit 1
fi
echo "✓ All checksums verified."
echo

echo "── Loading ANILA images into local docker ──"
for tar in 01-anila-built.tar.gz 02-base.tar.gz 03-cold.tar.gz; do
    echo "▶ $tar"
    gunzip -c "$tar" | docker load
done
if [ -f 04-models.tar.gz ]; then
    echo "▶ 04-models.tar.gz (optional model bundle)"
    gunzip -c 04-models.tar.gz | docker load
fi
echo

# ── 正式 platform image closure 驗證 ─────────────────────────────────────
# default service 缺 image = 整包不可部署,直接 fail。profile image 是刻意可選,
# 缺少只警告；bundle exporter 正常會把它收進 03-cold。
required_missing=0
optional_missing=0
echo "── Verifying formal platform image inventory ──"
while IFS=$'\t' read -r service image bundle activation source; do
    [ -z "${service:-}" ] && continue
    [[ "$service" == \#* ]] && continue
    lock_row="$(awk -F '\t' -v wanted="$service" '
        $0 !~ /^#/ && $1 == wanted { print; count++ }
        END { if (count != 1) exit 2 }
    ' PLATFORM-IMAGE-LOCK.tsv)" || {
        echo "  ✗ LOCK missing/duplicate: $service"
        required_missing=$((required_missing + 1))
        continue
    }
    IFS=$'\t' read -r lock_service lock_image expected_id lock_bundle lock_activation \
        <<< "$lock_row"
    if [ "$lock_image" != "$image" ] || [ "$lock_bundle" != "$bundle" ] \
       || [ "$lock_activation" != "$activation" ]; then
        echo "  ✗ LOCK metadata mismatch: $service"
        required_missing=$((required_missing + 1))
        continue
    fi
    actual_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
    if [ -n "$actual_id" ] && [ "$actual_id" = "$expected_id" ]; then
        echo "  ✓ $service -> $image @ $expected_id ($activation)"
    elif [ "$activation" = default ]; then
        echo "  ✗ REQUIRED missing/stale: $service -> $image"
        echo "    expected $expected_id, actual ${actual_id:-missing}, bundle $bundle"
        required_missing=$((required_missing + 1))
    else
        echo "  ⚠ optional missing/stale: $service -> $image ($activation)"
        optional_missing=$((optional_missing + 1))
    fi
done < PLATFORM-IMAGE-INVENTORY.tsv
[ "$required_missing" -eq 0 ] || {
    echo "✗ $required_missing required formal platform image(s) missing;拒絕宣告 load 完成"
    exit 1
}
[ "$optional_missing" -eq 0 ] \
    || echo "⚠ $optional_missing optional profile image(s) missing;default stack 不受影響"
echo "✓ Formal default platform image closure verified."
echo

if [ -f MODEL-IMAGE-STATUS.tsv ]; then
    echo "── Optional model image status (not part of formal platform closure) ──"
    cat MODEL-IMAGE-STATUS.tsv
    if grep -Eq $'\t(missing|skipped)\t' MODEL-IMAGE-STATUS.tsv; then
        echo "⚠ optional model bundle 並非完整本機模型部署;依實際拓撲補 image/權重"
    fi
    echo
fi

# ── HF 權重 (05-weights-*.tar,WITH_WEIGHTS=1 打包時才有) ────────────────
# 解到 ANILA_HF_DIR — 慣例是 <repo>/models/model (infra/models/
# docker-compose.yml 的權重掛載預設)。先 export ANILA_HF_DIR=<repo>/models/model
# 再跑本腳本;放別處就之後在 .env 設同一個值。
HF_DIR="${ANILA_HF_DIR:-$HOME/project/Huggingface}"
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
docker images | grep -E "anila-platform|pgvector|redis|nginx|code-server|n8n|gitlab|tensorrt-llm-hf|vllm-gemma4|tritonserver|embedding-proxy|flux2-dev|anila-flux-agent" || true
echo
echo "✓ Load complete. 後續步驟:"
echo "   cd <repo-root>"
echo "   docker network create anila-models-net  # 若還沒建"
echo "   bash infra/deployment/intranet/intranet-deploy.sh"
echo "   # 正式入口會把 bundle 的 PLATFORM-IMAGE-LOCK.tsv 寫成 ANILA_IMAGE_* content-ID lock。"
echo "   # 不要在 lock 寫入前直接 docker compose up；compose 會 fail-closed。"
echo "   # code-server 為 optional profile: deploy-prod.sh codeserver-up"
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
