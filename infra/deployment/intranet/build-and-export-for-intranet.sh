#!/usr/bin/env bash
# build-and-export-for-intranet.sh
# ============================================================================
# 從「有效 compose 組態」衍生要打包的 image 清單 → 確認本機都有 →
# docker save 成 tar.gz,帶進無外網的內網主機 docker load。
#
# ⚠ 清單不再手寫。新增 compose service 會自動進 bundle;漏包只能發生在
#   「本機根本沒有那張 image」,那時腳本會大聲失敗而不是靜默略過。
#
# 用法 (在有外網 / 已 build 好的機器執行):
#   bash infra/deployment/intranet/build-and-export-for-intranet.sh [OUTPUT_DIR]
#   OUTPUT_DIR 預設 /tmp/anila-images-export
#
# 環境變數:
#   COMPOSE_PROJECT_NAME  compose project(-p)。決定 build 出來的 image 前綴
#                         (例如 anila-restart-csp)。預設 anila-restart。
#                         內網 up 時必須用同一個 -p,否則找不到 image。
#   COMPOSE_ENV_FILE      給 compose 插值用的 env 檔。預設 $REPO_ROOT/.env。
#                         worktree 預演可指到主樹 .env(只讀插值,不寫入)。
#   INCLUDE_ASR=1         預設 ON。把 --profile asr 算進有效組態,bundle 會含
#                         asr-gateway / asr-decoder。高階審查與本機驗證棧都
#                         開了語音;關掉才設 INCLUDE_ASR=0。
#                         註:asr-cpu.yml 只改 deploy/device,不改 image 名,
#                         打包不必帶;內網 .15 有 GPU 時用平台預設即可。
#   COMPOSE_EXTRA_FILES   額外 -f 檔(空白分隔),接在 compose.yaml 後面。
#                         例:本機 CPU 語音預演可設
#                         COMPOSE_EXTRA_FILES=infra/compose/asr-cpu.yml
#                         (仍不改 image 清單,只影響 config 其他欄位)。
#   SKIP_BUILD=1          不跑 docker compose build(預演 / 已有映像時用)。
#                         預設 0=會 build 有效組態裡有 build: 的服務。
#   SKIP_PULL=1           不跑 docker pull。缺的上游 image 直接失敗。
#                         預設 0=對「非本專案 build」的缺圖嘗試 pull。
#   REBUILD_ON_SAVE_FAIL=1  若 docker save 被本機 IDS 毒到的 overlay 擋下,
#                         對「有 build: 的服務」立刻 compose build --no-cache
#                         該服務並馬上再 save(搶在 IDS 再次掃描前)。
#                         預設 0。不影響上游 image(pg/redis/…);那些 save
#                         失敗就直接 abort。
#                         ⚠ 若服務的 image: 寫死共用 tag(如 asr-decoder 的
#                         anila/asr-decoder:0.1.0、未 overlay 的
#                         anila-codeserver:local),--no-cache build 會 retag
#                         正在跑的那張 — 驗證棧不能動時不要開,或先用
#                         COMPOSE_EXTRA_FILES 把 image 名改到獨立命名空間。
#   WITH_MODELS=1         另打包 04-models.tar.gz(數十 GB)。預設 OFF。
#   WITH_WEIGHTS=1        另打包 05-weights-*.tar(數百 GB)。預設 OFF。
#   WEIGHTS_LIST / ANILA_HF_DIR  權重清單與來源,見舊註解。
#
# 輸出:
#   $OUTPUT_DIR/
#     ├── 01-images/<safe>.tar.gz   (有效 compose 每一張 image 一檔;可續傳)
#     ├── 01-compose-images.images.txt
#     ├── 04-models.tar.gz          (僅 WITH_MODELS=1)
#     ├── 05-weights-*.tar          (僅 WITH_WEIGHTS=1)
#     ├── CHECKSUMS.sha256
#     ├── INTRANET-LOAD.sh
#     └── MANIFEST.txt              (檔案大小 + sha256 + 每張 image 的 RepoDigest/Id)
#
# 內網端:
#   bash INTRANET-LOAD.sh
#   然後用同一個 COMPOSE_PROJECT_NAME 起棧(見 docs/runbooks/intranet-image-bundle.md)
# ============================================================================
set -euo pipefail

OUTPUT_DIR="${1:-/tmp/anila-images-export}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-anila-restart}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-$REPO_ROOT/.env}"
INCLUDE_ASR="${INCLUDE_ASR:-1}"
SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_PULL="${SKIP_PULL:-0}"
REBUILD_ON_SAVE_FAIL="${REBUILD_ON_SAVE_FAIL:-0}"

cd "$REPO_ROOT"
mkdir -p "$OUTPUT_DIR"

# ── compose 引數(單一真相來源)──────────────────────────────────────────
COMPOSE_FILES=(-f compose.yaml)
# 額外 overlay(空白分隔路徑,相對 REPO_ROOT 或絕對路徑)
if [ -n "${COMPOSE_EXTRA_FILES:-}" ]; then
    # shellcheck disable=SC2086
    for extra in $COMPOSE_EXTRA_FILES; do
        COMPOSE_FILES+=(-f "$extra")
    done
fi
COMPOSE_PROFILE_ARGS=()
if [ "$INCLUDE_ASR" = "1" ]; then
    COMPOSE_PROFILE_ARGS=(--profile asr)
fi

compose() {
    docker compose --env-file "$COMPOSE_ENV_FILE" -p "$COMPOSE_PROJECT_NAME" \
        "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILE_ARGS[@]}" "$@"
}

die() { echo "✗ $*" >&2; exit 1; }

echo "============================================================"
echo "ANILA — Build & Export for Intranet"
echo "  Repo:       $REPO_ROOT"
echo "  Output:     $OUTPUT_DIR"
echo "  Project:    $COMPOSE_PROJECT_NAME  (-p;内網 up 必須同名)"
echo "  Env file:   $COMPOSE_ENV_FILE"
echo "  INCLUDE_ASR:$INCLUDE_ASR  (1 → --profile asr 納入有效組態)"
echo "  SKIP_BUILD: $SKIP_BUILD   SKIP_PULL: $SKIP_PULL"
echo "  REBUILD_ON_SAVE_FAIL: $REBUILD_ON_SAVE_FAIL"
echo "============================================================"
echo

[ -f "$COMPOSE_ENV_FILE" ] || die "COMPOSE_ENV_FILE 不存在:$COMPOSE_ENV_FILE(compose 插值需要它)"

# ── Phase 0: 從有效 compose 組態衍生 image 清單 ─────────────────────────
echo "▶ [0/5] Deriving image list from effective compose config..."
echo "  \$ docker compose -p $COMPOSE_PROJECT_NAME ${COMPOSE_FILES[*]} ${COMPOSE_PROFILE_ARGS[*]:-} config --images"
mapfile -t RAW_IMAGES < <(compose config --images | sed '/^$/d')
[ ${#RAW_IMAGES[@]} -gt 0 ] || die "compose config --images 回傳空清單"

# 去重、排序(codeserver-init 與 codeserver 共用同一 tag)
mapfile -t IMAGES < <(printf '%s\n' "${RAW_IMAGES[@]}" | sort -u)

echo "  Effective images (${#IMAGES[@]} unique):"
for img in "${IMAGES[@]}"; do
    echo "    - $img"
done
echo

# 任何 anila-platform- 殘留都是腳本或 project 名設錯
for img in "${IMAGES[@]}"; do
    case "$img" in
        anila-platform-*)
            die "衍生清單出現過期前綴 anila-platform-*:$img — 請設 COMPOSE_PROJECT_NAME 對齊本機棧"
            ;;
    esac
done

# 服務 → image 對照(寫進 MANIFEST,給 IT / 預演驗收用)。
# compose config --format json 對「只有 build:、沒寫 image:」的服務會給 null,
# 但實際 tag 是 {project}-{service}(與 config --images 一致)。
SERVICE_IMAGE_MAP="$(
    COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" compose config --format json | python3 -c '
import json, os, sys
project = os.environ["COMPOSE_PROJECT_NAME"]
cfg = json.load(sys.stdin)
services = cfg.get("services") or {}
for name in sorted(services):
    img = services[name].get("image") or ""
    if not img and services[name].get("build") is not None:
        img = f"{project}-{name}"
    print(f"{name}\t{img}")
')"

# ── Phase 1: build(可跳過)─────────────────────────────────────────────
if [ "$SKIP_BUILD" = "1" ]; then
    echo "▶ [1/5] Build skipped (SKIP_BUILD=1)"
else
    echo "▶ [1/5] Building services with a build section via docker compose..."
    # 不列服務名 — compose 自己知道誰有 build:;避免再手寫一份會過期的清單
    compose build
    echo "✓ Built."
fi
echo

# ── Phase 2: 確認每張 image 都在本機;缺的上游可 pull,否則失敗 ──────────
echo "▶ [2/5] Ensuring every derived image exists locally..."
MISSING=()
for img in "${IMAGES[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then
        echo "  ✓ $img"
        continue
    fi
    # 專案 build 出來的 image(前綴 = project name)沒有 registry 可 pull
    if [[ "$img" == "${COMPOSE_PROJECT_NAME}-"* ]] || [[ "$img" == anila-codeserver:* ]] || [[ "$img" == anila/* ]]; then
        echo "  ✗ MISSING (local build/tag): $img"
        MISSING+=("$img")
        continue
    fi
    if [ "$SKIP_PULL" = "1" ]; then
        echo "  ✗ MISSING (SKIP_PULL=1, not pulling): $img"
        MISSING+=("$img")
        continue
    fi
    echo "  → pulling $img"
    if ! docker pull "$img"; then
        echo "  ✗ pull failed: $img"
        MISSING+=("$img")
    else
        echo "  ✓ pulled $img"
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    echo
    echo "============================================================"
    echo "✗ REFUSING TO EXPORT — missing images would produce a short bundle."
    echo "  The following images are named by the effective compose config"
    echo "  but are not present locally:"
    for img in "${MISSING[@]}"; do
        echo "    - $img"
    done
    echo
    echo "  Fix: build/tag them (SKIP_BUILD=0) or pull upstream, then re-run."
    echo "============================================================"
    exit 1
fi
echo "✓ All ${#IMAGES[@]} images present."
echo

# ── Phase 3: 逐張 save(一 image 一檔;點名失敗;可續傳)──────────────────
echo "▶ [3/5] Saving compose images → 01-images/*.tar.gz ..."
IMG_DIR="$OUTPUT_DIR/01-images"
mkdir -p "$IMG_DIR"
{
    printf '%s\n' "${IMAGES[@]}"
} > "$OUTPUT_DIR/01-compose-images.images.txt"

# image → 擁有它的 compose service 名(供 REBUILD_ON_SAVE_FAIL)
declare -A IMAGE_TO_SERVICE=()
while IFS=$'\t' read -r svc img; do
    [ -n "$img" ] || continue
    # 同一 image 可能被多個 service 共用(codeserver-init/codeserver);留一個即可
    IMAGE_TO_SERVICE["$img"]="$svc"
done <<<"$SERVICE_IMAGE_MAP"

# 先 docker save -o 成未壓縮 tar,再 gzip。pipe 拉長 save 時間,本機 IDS
# 更容易在中途把 overlay 弄壞;分兩步比較搶得過。
save_one_image() {
    # $1=image  $2=out.tar.gz  → 0/1
    local img="$1" out="$2" err raw
    err="$(mktemp)"
    raw="$(mktemp --suffix=.tar)"
    if ! docker save "$img" -o "$raw" 2>"$err"; then
        echo "FAIL"
        echo "    $(tr '\n' ' ' <"$err")"
        rm -f "$err" "$raw" "$out"
        return 1
    fi
    if [ ! -s "$raw" ] || [ "$(stat -c%s "$raw")" -lt 1024 ]; then
        echo "FAIL (raw tar too small)"
        rm -f "$err" "$raw" "$out"
        return 1
    fi
    if ! gzip -c "$raw" >"$out"; then
        echo "FAIL (gzip)"
        rm -f "$err" "$raw" "$out"
        return 1
    fi
    rm -f "$err" "$raw"
    echo "OK ($(du -h "$out" | cut -f1))"
    return 0
}

SAVE_FAIL=()
: > "$OUTPUT_DIR/01-compose-images.files.txt"
for img in "${IMAGES[@]}"; do
    safe="$(printf '%s' "$img" | tr '/:' '__')"
    out="$IMG_DIR/$safe.tar.gz"
    echo -n "  save $img → 01-images/$safe.tar.gz ... "
    if save_one_image "$img" "$out"; then
        echo "$safe.tar.gz	$img" >> "$OUTPUT_DIR/01-compose-images.files.txt"
        continue
    fi

    if [ "$REBUILD_ON_SAVE_FAIL" != "1" ]; then
        SAVE_FAIL+=("$img")
        continue
    fi
    svc="${IMAGE_TO_SERVICE[$img]:-}"
    if [ -z "$svc" ]; then
        echo "    REBUILD_ON_SAVE_FAIL: no buildable service owns $img — cannot recover"
        SAVE_FAIL+=("$img")
        continue
    fi

    recovered=0
    for attempt in 1 2 3; do
        echo "    → REBUILD_ON_SAVE_FAIL attempt $attempt/3: compose build --no-cache $svc"
        if ! compose build --no-cache "$svc"; then
            echo "    ✗ rebuild failed for service $svc"
            continue
        fi
        echo -n "    re-save $img ... "
        if save_one_image "$img" "$out"; then
            echo "$safe.tar.gz	$img" >> "$OUTPUT_DIR/01-compose-images.files.txt"
            recovered=1
            break
        fi
    done
    if [ "$recovered" -ne 1 ]; then
        SAVE_FAIL+=("$img")
    fi
done

if [ ${#SAVE_FAIL[@]} -gt 0 ]; then
    echo
    echo "============================================================"
    echo "✗ REFUSING TO EXPORT — docker save failed for:"
    for img in "${SAVE_FAIL[@]}"; do
        echo "    - $img"
    done
    echo
    echo "  Common cause on this host: host IDS (sisidsdaemon) poisons overlay"
    echo "  merged/ views so docker save fails for affected images."
    echo "  Fix A (recommended tomorrow): stop the stack, re-run, then up."
    echo "  Fix B (stack must stay up): REBUILD_ON_SAVE_FAIL=1 with a separate"
    echo "  COMPOSE_PROJECT_NAME (+ codeserver image overlay); see runbook §8."
    echo "  Do NOT ship a partial bundle."
    echo "============================================================"
    exit 1
fi

# 確認 images.txt 與實際檔案 1:1
while IFS= read -r img; do
    safe="$(printf '%s' "$img" | tr '/:' '__')"
    [ -f "$IMG_DIR/$safe.tar.gz" ] || die "missing $IMG_DIR/$safe.tar.gz for $img"
done < "$OUTPUT_DIR/01-compose-images.images.txt"
echo "  ✓ $(du -sh "$IMG_DIR" | cut -f1) across $(find "$IMG_DIR" -name '*.tar.gz' | wc -l) files"
echo

# ── Phase 4: model image(預設跳過;開了也 fail-loud,不再靜默 skip)─────
if [ "${WITH_MODELS:-0}" = "1" ]; then
    echo "  • 04-models.tar.gz (WITH_MODELS=1)"
    MODEL_IMAGES=(
        tensorrt-llm-hf:1.3.0rc10
        vllm-gemma4:latest
        tritonserver:25.04-nv-embed-v2
        embedding-proxy:migration
        flux2-dev:bf16
        anila-flux-agent:latest
    )
    MODEL_MISSING=()
    for img in "${MODEL_IMAGES[@]}"; do
        if docker image inspect "$img" >/dev/null 2>&1; then
            echo "    ✓ $img"
        else
            echo "    ✗ MISSING: $img"
            MODEL_MISSING+=("$img")
        fi
    done
    if [ ${#MODEL_MISSING[@]} -gt 0 ]; then
        die "WITH_MODELS=1 but missing: ${MODEL_MISSING[*]}"
    fi
    docker save "${MODEL_IMAGES[@]}" | gzip > "$OUTPUT_DIR/04-models.tar.gz"
    printf '%s\n' "${MODEL_IMAGES[@]}" > "$OUTPUT_DIR/04-models.images.txt"
    echo "    ✓ Saved ${#MODEL_IMAGES[@]} model image(s)"
else
    echo "  • 04-models.tar.gz — skipped (WITH_MODELS=0)"
fi
echo

# ── Phase 4b: HF 權重 ───────────────────────────────────────────────────
if [ "${WITH_WEIGHTS:-0}" = "1" ]; then
    HF_DIR="${ANILA_HF_DIR:-/home/aia/c1147259/project/Huggingface}"
    WEIGHTS_LIST="${WEIGHTS_LIST:-FLUX.2-dev gemma-4-31B-it gemma-4-31B-it-assistant}"
    echo "  • 05-weights-*.tar (WITH_WEIGHTS=1,來源 $HF_DIR)"
    WEIGHT_MISSING=()
    for w in $WEIGHTS_LIST; do
        if [ -d "$HF_DIR/$w" ]; then
            echo "    - $w ($(du -sh "$HF_DIR/$w" | cut -f1))"
            tar -cf "$OUTPUT_DIR/05-weights-${w}.tar" -C "$HF_DIR" "$w"
        else
            echo "    ✗ MISSING: $HF_DIR/$w"
            WEIGHT_MISSING+=("$HF_DIR/$w")
        fi
    done
    if [ ${#WEIGHT_MISSING[@]} -gt 0 ]; then
        die "WITH_WEIGHTS=1 but missing: ${WEIGHT_MISSING[*]}"
    fi
else
    echo "  • 05-weights-*.tar — skipped (WITH_WEIGHTS=0)"
fi
echo

# ── Phase 5: MANIFEST + INTRANET-LOAD.sh ────────────────────────────────
echo "▶ [4/5] Writing MANIFEST.txt + INTRANET-LOAD.sh..."

(
    cd "$OUTPUT_DIR"
    shopt -s nullglob
    sha256sum 01-images/*.tar.gz 04-models.tar.gz 05-weights-*.tar 2>/dev/null > CHECKSUMS.sha256 \
        || sha256sum 01-images/*.tar.gz > CHECKSUMS.sha256
)

{
    echo "ANILA Platform — Intranet Image Bundle"
    echo "Built at: $(date -Iseconds)"
    echo "Built by: $(whoami)@$(hostname)"
    echo "Repo:     $REPO_ROOT"
    echo "Branch:   $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'n/a')"
    echo "Commit:   $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'n/a')"
    echo "Project:  $COMPOSE_PROJECT_NAME"
    echo "INCLUDE_ASR: $INCLUDE_ASR"
    echo "Compose:  ${COMPOSE_FILES[*]} ${COMPOSE_PROFILE_ARGS[*]:-}"
    echo
    echo "── Service → image (from compose config) ──────────────"
    echo "$SERVICE_IMAGE_MAP"
    echo
    echo "── Images in 01-images/*.tar.gz ───────────────────────"
    while IFS=$'\t' read -r file img; do
        [ -n "$img" ] || continue
        meta="$(docker image inspect "$img" --format '{{.Id}} {{.Size}} {{json .RepoDigests}}' 2>/dev/null || echo '? ? []')"
        id="$(awk '{print $1}' <<<"$meta")"
        bytes="$(awk '{print $2}' <<<"$meta")"
        digests="$(awk '{$1="";$2=""; sub(/^  /,""); print}' <<<"$meta")"
        hr="$(numfmt --to=iec --suffix=B "$bytes" 2>/dev/null || echo "${bytes}B")"
        fbytes="$(stat -c%s "$IMG_DIR/$file" 2>/dev/null || echo 0)"
        fhr="$(numfmt --to=iec --suffix=B "$fbytes" 2>/dev/null || echo "${fbytes}B")"
        echo "  $img"
        echo "    Archive: 01-images/$file ($fhr, $fbytes bytes)"
        echo "    Id:      $id"
        echo "    Size:    $hr ($bytes bytes)"
        echo "    Digests: $digests"
    done < "$OUTPUT_DIR/01-compose-images.files.txt"
    if [ -f "$OUTPUT_DIR/04-models.images.txt" ]; then
        echo
        echo "── Images in 04-models.tar.gz ─────────────────────────"
        while IFS= read -r img; do
            meta="$(docker image inspect "$img" --format '{{.Id}} {{.Size}} {{json .RepoDigests}}' 2>/dev/null || echo '? ? []')"
            id="$(awk '{print $1}' <<<"$meta")"
            bytes="$(awk '{print $2}' <<<"$meta")"
            digests="$(awk '{$1="";$2=""; sub(/^  /,""); print}' <<<"$meta")"
            hr="$(numfmt --to=iec --suffix=B "$bytes" 2>/dev/null || echo "${bytes}B")"
            echo "  $img"
            echo "    Id:      $id"
            echo "    Size:    $hr ($bytes bytes)"
            echo "    Digests: $digests"
        done < "$OUTPUT_DIR/04-models.images.txt"
    fi
    echo
    echo "── Bundle files ───────────────────────────────────────"
    du -sh "$OUTPUT_DIR" "$IMG_DIR"
    ls -lh "$IMG_DIR"
    ls -lh "$OUTPUT_DIR"/*.{txt,sha256,sh,tar.gz,tar} 2>/dev/null || true
    echo
    echo "── SHA256 (IT 對檔;機器驗檔用 CHECKSUMS.sha256) ──────"
    cat "$OUTPUT_DIR/CHECKSUMS.sha256"
} > "$OUTPUT_DIR/MANIFEST.txt"

# INTRANET-LOAD.sh:載入 01-images/*.tar.gz(+ 可選 04-models)
cat > "$OUTPUT_DIR/INTRANET-LOAD.sh" <<EOF
#!/usr/bin/env bash
# INTRANET-LOAD.sh — 內網端 docker load。由此次 export 產生,勿手改檔名清單。
# 執行前確認:
#   1. docker 已裝且能跑 (docker info 不報錯)
#   2. 同目錄有 01-images/*.tar.gz + CHECKSUMS.sha256 + 01-compose-images.images.txt
#   3. repo 已在內網機器就緒,且 up 時 -p 與打包時 COMPOSE_PROJECT_NAME 相同
set -euo pipefail
cd "\$(dirname "\${BASH_SOURCE[0]}")"

if [ -f CHECKSUMS.sha256 ]; then
    echo "── Verifying SHA256 checksums ──"
    if ! sha256sum -c CHECKSUMS.sha256; then
        echo
        echo "✗ Checksum mismatch — 拒絕 load。請重新取得 bundle。"
        exit 1
    fi
    echo "✓ All checksums verified."
    echo
else
    echo "⚠ CHECKSUMS.sha256 不存在 — 略過完整性檢查(不建議在 prod 用)"
    echo
fi

echo "── Checking expected per-image tarballs exist ──"
[ -f 01-compose-images.files.txt ] || { echo "✗ missing 01-compose-images.files.txt"; exit 1; }
while IFS=\$'\\t' read -r file img; do
    [ -n "\$file" ] || continue
    if [ ! -f "01-images/\$file" ]; then
        echo "✗ missing 01-images/\$file (for \$img)"
        exit 1
    fi
    echo "  ✓ 01-images/\$file  (\$img)"
done < 01-compose-images.files.txt
echo

echo "── Loading ANILA images into local docker ──"
while IFS=\$'\\t' read -r file img; do
    [ -n "\$file" ] || continue
    echo "▶ 01-images/\$file  (\$img)"
    gunzip -c "01-images/\$file" | docker load
done < 01-compose-images.files.txt

if [ -f 04-models.tar.gz ]; then
    echo "▶ 04-models.tar.gz"
    gunzip -c 04-models.tar.gz | docker load
fi
echo

HF_DIR="\${ANILA_HF_DIR:-/home/aia/c1147259/project/Huggingface}"
shopt -s nullglob
WEIGHT_TARS=(05-weights-*.tar)
if [ \${#WEIGHT_TARS[@]} -gt 0 ]; then
    echo "── Extracting model weights → \$HF_DIR ──"
    mkdir -p "\$HF_DIR"
    for tar in "\${WEIGHT_TARS[@]}"; do
        echo "▶ \$tar"
        tar -xf "\$tar" -C "\$HF_DIR"
    done
    echo
fi

echo "── Verifying loaded tags (from *.images.txt) ──"
shopt -s nullglob
for list in *.images.txt; do
    while IFS= read -r img; do
        [ -z "\$img" ] && continue
        if docker image inspect "\$img" >/dev/null 2>&1; then
            echo "  ✓ \$img"
        else
            echo "  ✗ missing after load: \$img"
            exit 1
        fi
    done < "\$list"
done
echo
echo "✓ Load complete."
echo "  下一步見 docs/runbooks/intranet-image-bundle.md"
echo "  起棧時 -p 必須是: $COMPOSE_PROJECT_NAME"
echo "  INCLUDE_ASR 打包值: $INCLUDE_ASR → up 時記得 --profile asr(若為 1)"
EOF
chmod +x "$OUTPUT_DIR/INTRANET-LOAD.sh"

echo "✓ Done."
echo
echo "▶ [5/5] Summary"
du -sh "$OUTPUT_DIR"
ls -lh "$OUTPUT_DIR"
echo
echo "============================================================"
echo "下一步:把 $OUTPUT_DIR/ 整個帶進內網,執行 bash INTRANET-LOAD.sh"
echo "起棧 -p 必須 = $COMPOSE_PROJECT_NAME"
echo "============================================================"
