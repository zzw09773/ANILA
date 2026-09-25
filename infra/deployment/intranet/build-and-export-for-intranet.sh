#!/usr/bin/env bash
# build-and-export-for-intranet.sh
# ============================================================================
# 從「有效 compose 組態」衍生要打包的 image 清單 → buildx / docker save
# 成 tar.gz,帶進無外網的內網主機 docker load。
#
# ⚠ 清單不再手寫。新增 compose service 會自動進 bundle;漏包只能發生在
#   「本機根本沒有那張 image」,那時腳本會大聲失敗而不是靜默略過。
#
# ⚠ buildx tar 產出之後有一道**建後雜物掃描**([2b/5],
#   infra/deployment/scripts/scan-image-artifacts.sh):本專案 build 出來的映像
#   若烘進私鑰 / 日誌 / secrets/ / 使用者附件 / 測試快取就**中止匯出**。
#   沒有跳過的旗標 —— 交付品出了門收不回來。修法見中止時印出的自救路徑。
#
# 用法 (在有外網 / 已 build 好的機器執行):
#   bash infra/deployment/intranet/build-and-export-for-intranet.sh [OUTPUT_DIR]
#   OUTPUT_DIR 預設 /tmp/anila-images-export
#
# 環境變數:
#   COMPOSE_PROJECT_NAME  compose project(-p)。決定 build 出來的 image 前綴
#                         (例如 anila-csp)。預設 anila。
#                         內網 up 時必須用同一個 -p,否則找不到 image。
#   COMPOSE_ENV_FILE      給 compose 插值用的 env 檔。預設 $REPO_ROOT/.env。
#                         worktree 預演可指到主樹 .env(只讀插值,不寫入)。
#   INCLUDE_ASR=1         預設 OFF。設 1 才把 --profile asr 算進有效組態,
#                         bundle 才含 asr-gateway / asr-decoder。平台開機
#                         預設沒語音(麥克風要 /asr/health 200 才出現);
#                         開語音是開機後第二步,見 intranet-image-bundle.md
#                         §5.1。若預知之後要開、不想再跑一趟打包,打包時才
#                         設 1(只帶映像);部署腳本預設仍是 0。
#                         註:asr-cpu.yml 只改 deploy/device,不改 image 名,
#                         打包不必帶;內網 .15 有 GPU 時用平台預設即可。
#   COMPOSE_EXTRA_FILES   額外 -f 檔(空白分隔),接在 compose.yaml 後面。
#                         例:本機 CPU 語音預演可設
#                         COMPOSE_EXTRA_FILES=infra/compose/asr-cpu.yml
#                         (仍不改 image 清單,只影響 config 其他欄位)。
#   SKIP_BUILD=1          不跑 buildx;若 OUTPUT_DIR/01-images/ 已有對應的
#                         built-image tar.gz 就沿用並記錄,缺任何一張便拒絕匯出。
#                         預設 0=依 compose build --print 的 Bake targets buildx 直出。
#                         對 WITH_DOCLING_IMAGE=1 同一語意:缺 docling tar 就死,
#                         不會改去 build 一張 10GB 的映像。
#                         ⚠ 重用 OUTPUT_DIR 時**不刪**上一輪的檔(刪掉操作者
#                         手上 5.8G 比缺陷危險)。改由 assert_checksum_matches_declared
#                         fail-loud:checksum glob 撿到的每一個檔必須能追溯到
#                         「這一次執行宣告要產出的東西」;意外檔點名後死亡。
#                         MANIFEST / INTRANET-LOAD 的旗標與來歷陳述只讀這次的
#                         旗標,不讀檔案在不在。
#   SKIP_PULL=1           不跑 docker pull。缺的上游 image 直接失敗。
#                         預設 0=對「非本專案 build」的缺圖嘗試 pull。
#   SCAN_SCRIPT           掃描器路徑,預設為 repo 內的 scanner;可在交叉封包
#                         contract 驗收時指向 scanner 複本。
#   WITH_MODELS=1         另打包 04-models.tar.gz(數十 GB)。預設 OFF。
#   WITH_WEIGHTS=1        另打包 05-weights-*.tar(數百 GB)。預設 OFF。
#   WEIGHTS_LIST / ANILA_HF_DIR  權重清單與來源,見舊註解。
#   WITH_DOCLING_WEIGHTS=1 另打包 05-weights-docling.tar。預設 OFF。
#   DOCLING_WEIGHTS_DIR    fetch-docling-weights.sh 產出的來源目錄。
#   WITH_DOCLING_IMAGE=1   把 docling 映像走完與其他 built image 相同的
#                         五段式（buildx bake 直出 tar → 掃 tar → 真的
#                         docker load 驗回）。預設 OFF。
#                         ⚠ 不把 --profile docling-local 加進平台有效組態
#                         （COMPOSE_PROFILE_ARGS）——那個 profile 是「平台
#                         主機永遠不起它」的機制；打包只另跑一次 bake
#                         print 抽 docling target。
#                         開啟時 GPU 主機四件套必須齊：映像 tar、權重 tar、
#                         docker-compose.standalone.yml、.env.example
#                         （後兩件 + README 收成 06-docling-gpu-host.tar）。
#                         因此 WITH_DOCLING_IMAGE=1 會一併把
#                         WITH_DOCLING_WEIGHTS 設成 1，並要求
#                         DOCLING_WEIGHTS_DIR。
#
# 輸出:
#   $OUTPUT_DIR/
#     ├── 01-images/<safe>.tar.gz   (有效 compose 每一張 image 一檔;可續傳)
#     ├── 01-compose-images.images.txt
#     ├── 04-models.tar.gz          (僅 WITH_MODELS=1)
#     ├── 05-weights-*.tar          (僅 WITH_WEIGHTS=1)
#     ├── 05-weights-docling.tar    (僅 WITH_DOCLING_WEIGHTS=1；IMAGE=1 時必有)
#     ├── 06-docling-gpu-host.tar   (僅 WITH_DOCLING_IMAGE=1)
#     ├── CHECKSUMS.sha256
#     ├── INTRANET-LOAD.sh
#     ├── intranet-image-overrides.yml (內網 up 時套用的 tag-only image override)
#     └── MANIFEST.txt              (檔案大小 + sha256 + 每張 image 的 RepoDigest/Id)
#
# 內網端:
#   bash INTRANET-LOAD.sh
#   然後用同一個 COMPOSE_PROJECT_NAME 起棧(見 docs/runbooks/intranet-image-bundle.md)
# ============================================================================
set -euo pipefail

OUTPUT_DIR="${1:-/tmp/anila-images-export}"
OUTPUT_DIR="${OUTPUT_DIR%/}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-anila}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-$REPO_ROOT/.env}"
INCLUDE_ASR="${INCLUDE_ASR:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_PULL="${SKIP_PULL:-0}"
WITH_DOCLING_WEIGHTS="${WITH_DOCLING_WEIGHTS:-0}"
DOCLING_WEIGHTS_DIR="${DOCLING_WEIGHTS_DIR:-}"
WITH_DOCLING_IMAGE="${WITH_DOCLING_IMAGE:-0}"
DOCLING_BAKE_FILE=""
DOCLING_IMAGE=""
BUILDER_NAME="anila-pkg"
BUILDX_CACHE_DIR="$(dirname "$OUTPUT_DIR")/$(basename "$OUTPUT_DIR").buildx-cache"

cd "$REPO_ROOT"
IMG_DIR="$OUTPUT_DIR/01-images"
mkdir -p "$OUTPUT_DIR" "$IMG_DIR"

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

if [ "$WITH_DOCLING_IMAGE" = "1" ]; then
    # GPU 主機四件套：映像 + 權重 + standalone yml + .env.example。
    WITH_DOCLING_WEIGHTS=1
    [ -n "$DOCLING_WEIGHTS_DIR" ] \
        || die "WITH_DOCLING_IMAGE=1 requires DOCLING_WEIGHTS_DIR (GPU-host four-piece set: image tar + weights tar + docker-compose.standalone.yml + .env.example)"
fi

echo "============================================================"
echo "ANILA — Build & Export for Intranet"
echo "  Repo:       $REPO_ROOT"
echo "  Output:     $OUTPUT_DIR"
echo "  Project:    $COMPOSE_PROJECT_NAME  (-p;内網 up 必須同名)"
echo "  Env file:   $COMPOSE_ENV_FILE"
echo "  INCLUDE_ASR:$INCLUDE_ASR  (0=預設不帶語音映像;1 → --profile asr 納入有效組態)"
echo "  WITH_DOCLING_IMAGE:$WITH_DOCLING_IMAGE  (1 → bake docling through five-stage; platform profile unchanged)"
echo "  WITH_DOCLING_WEIGHTS:$WITH_DOCLING_WEIGHTS"
echo "  SKIP_BUILD: $SKIP_BUILD   SKIP_PULL: $SKIP_PULL"
echo "  Buildx:      $BUILDER_NAME  (docker-container)"
echo "  Buildx cache: $BUILDX_CACHE_DIR"
echo "============================================================"
echo

if [ ! -f "$COMPOSE_ENV_FILE" ]; then
    echo "✗ COMPOSE_ENV_FILE 不存在:$COMPOSE_ENV_FILE(compose 插值需要它)" >&2
    echo "  worktree 執行時用 COMPOSE_ENV_FILE=<主樹>/.env 指向主樹的環境檔." >&2
    exit 1
fi

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

tag_only_image_ref() {
    case "$1" in
        *@sha256:*) printf '%s\n' "${1%@sha256:*}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# docker load 只會還原 tag,不會替 image 建立 RepoDigest。內網由 bundle 的
# CHECKSUMS + MANIFEST 完整性鏈承擔,所以另產一份只在內網 up 時疊加的 override。
INTRANET_IMAGE_OVERRIDES="$OUTPUT_DIR/intranet-image-overrides.yml"
SERVICE_IMAGE_MAP="$SERVICE_IMAGE_MAP" python3 - "$INTRANET_IMAGE_OVERRIDES" <<'PY'
import os
import sys

output = sys.argv[1]
pinned = []
for line in os.environ.get("SERVICE_IMAGE_MAP", "").splitlines():
    if not line:
        continue
    try:
        service, image = line.split("\t", 1)
    except ValueError as exc:
        raise SystemExit(f"service image map 格式錯誤:{line!r}") from exc
    if "@sha256:" not in image:
        continue
    tag_only = image.split("@sha256:", 1)[0]
    if not tag_only:
        raise SystemExit(f"digest-pinned image 沒有 tag:{service}:{image}")
    pinned.append((service, image, tag_only))

with open(output, "w", encoding="utf-8") as handle:
    handle.write("# 內網專用 image override：只把 compose 中含 @sha256: 的 service image 改成 tag-only。\n")
    handle.write("# RepoDigests 是 pull 的產物；docker load 不會重建 RepoDigests。\n")
    handle.write("# 內網完整性由交付包的 sha256 鏈（CHECKSUMS + MANIFEST）承擔。\n")
    handle.write("# 此檔只在內網 up 時疊加，不改 build host 的 digest pin。\n")
    handle.write("services:\n")
    if pinned:
        for service, _image, tag_only in pinned:
            handle.write(f"  {service}:\n")
            handle.write(f"    image: {tag_only}\n")
    else:
        handle.write("  {}\n")

for service, image, tag_only in pinned:
    print(f"  ✓ {service}: {image} → {tag_only}")
if not pinned:
    print("  ✓ No digest-pinned service images; wrote empty services override.")
PY
echo "  ✓ Intranet image override: $INTRANET_IMAGE_OVERRIDES"

# ── Phase 1: compose Bake → buildx tar(可跳過,可沿用既有 tar)──────────
echo "▶ [1/5] Deriving Compose Bake definition with docker compose build --print..."
BAKE_FILE="$(mktemp --suffix=.json)"
if ! compose build --print > "$BAKE_FILE"; then
    rm -f "$BAKE_FILE"
    die "docker compose build --print 失敗"
fi

# Compose 的 Bake default group 才是 build 集合;同一 tag(例如
# codeserver-init/codeserver)只輸出一次。這裡沒有服務名或 image 名清單。
BAKE_TARGET_MAP_TEXT="$(python3 - "$BAKE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)

targets = document.get("target") or {}
default_targets = ((document.get("group") or {}).get("default") or {}).get("targets") or []
if not default_targets:
    raise SystemExit("Bake definition 沒有 default targets")

chosen = {}
for target_name in default_targets:
    target = targets.get(target_name)
    if not isinstance(target, dict):
        raise SystemExit(f"Bake target 不存在:{target_name}")
    tags = target.get("tags")
    if not isinstance(tags, list) or len(tags) != 1 or not isinstance(tags[0], str) or not tags[0]:
        raise SystemExit(f"Bake target 必須有一個 image tag:{target_name}")
    if "${" in json.dumps(target, ensure_ascii=False):
        raise SystemExit(f"Bake target 仍有未解析的插值:{target_name}")
    chosen.setdefault(tags[0], target_name)

for image, target_name in sorted(chosen.items()):
    print(f"{target_name}\t{image}")
PY
)"
[ -n "$BAKE_TARGET_MAP_TEXT" ] || die "Bake definition 沒有可匯出的 target"

# 這是 build args 的 contract guard:Compose --print 必須把 anilalm / anila-ui
# 的值寫進 Bake JSON,不能把 ${...} 原樣交給後續 buildx。
python3 - "$BAKE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    targets = (json.load(handle).get("target") or {})
for name in ("anilalm", "anila-ui"):
    target = targets.get(name)
    if not isinstance(target, dict) or not isinstance(target.get("args"), dict):
        raise SystemExit(f"Bake build args 不存在:{name}")
    if "${" in json.dumps(target["args"], ensure_ascii=False):
        raise SystemExit(f"Bake build args 尚有未解析插值:{name}")
    print(f"  ✓ Bake interpolation: {name} args resolved ({', '.join(sorted(target['args']))})")
PY

mapfile -t BAKE_TARGET_MAP <<< "$BAKE_TARGET_MAP_TEXT"
BUILT_IMAGES=()
declare -A BUILT_IMAGE_SET=()
while IFS=$'\t' read -r target img; do
    [ -n "$target" ] || continue
    BUILT_IMAGE_SET["$img"]=1
    BUILT_IMAGES+=("$img")
done <<< "$BAKE_TARGET_MAP_TEXT"
[ ${#BUILT_IMAGES[@]} -gt 0 ] || die "Bake definition 衍生不到 built image"

# 再用有效 Compose JSON 交叉核對 Bake 的 unique tags,避免 Bake 輸出被錯誤
# 改寫或漏 target 時仍然繼續。兩邊都由 Compose 組態衍生,不是手寫清單。
EXPECTED_BUILT_IMAGES_TEXT="$(
    COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" compose config --format json | python3 -c '
import json, os, sys
project = os.environ["COMPOSE_PROJECT_NAME"]
cfg = json.load(sys.stdin)
images = set()
for name, svc in (cfg.get("services") or {}).items():
    if svc.get("build") is not None:
        images.add(svc.get("image") or f"{project}-{name}")
print("\n".join(sorted(images)))
'
)"
[ -n "$EXPECTED_BUILT_IMAGES_TEXT" ] || die "有效 Compose 組態沒有 build: image"
ACTUAL_BUILT_IMAGES_TEXT="$(printf '%s\n' "${BUILT_IMAGES[@]}")"
if [ "$EXPECTED_BUILT_IMAGES_TEXT" != "$ACTUAL_BUILT_IMAGES_TEXT" ]; then
    echo "Compose build set:" >&2
    printf '%s\n' "$EXPECTED_BUILT_IMAGES_TEXT" >&2
    echo "Bake build set:" >&2
    printf '%s\n' "$ACTUAL_BUILT_IMAGES_TEXT" >&2
    die "Compose build set 與 Bake target tags 不一致"
fi
for img in "${BUILT_IMAGES[@]}"; do
    [ -n "${BUILT_IMAGE_SET[$img]+x}" ] || die "內部錯誤: built image set 缺少 $img"
done
echo "  ✓ Compose/Bake build set: ${#BUILT_IMAGES[@]} unique image(s)"

declare -A BUILT_TAR_PATH=()

ensure_buildx_builder() {
    local driver inspect_output
    if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
        echo "  → creating builder $BUILDER_NAME (docker-container)"
        docker buildx create --name "$BUILDER_NAME" --driver docker-container >/dev/null \
            || die "無法建立 buildx builder:$BUILDER_NAME"
    fi
    inspect_output="$(docker buildx inspect "$BUILDER_NAME" 2>/dev/null)"
    driver="$(awk -F': *' '/^Driver:/ {print $2}' <<< "$inspect_output")"
    [ "$driver" = "docker-container" ] \
        || die "builder $BUILDER_NAME 不是 docker-container(driver=$driver),拒絕繼承其他 builder"
    mkdir -p "$BUILDX_CACHE_DIR"
    docker buildx inspect --bootstrap "$BUILDER_NAME" >/dev/null \
        || die "buildx builder bootstrap 失敗:$BUILDER_NAME"
    echo "  ✓ builder $BUILDER_NAME ready (docker-container; cache=$BUILDX_CACHE_DIR)"
}

buildx_export_one() {
    # $1=image  $2=bake-target  $3=out.tar.gz  [$4=bake-file] → 0/1
    local img="$1" target="$2" out="$3" bake_file="${4:-$BAKE_FILE}" safe raw compressed
    safe="$(printf '%s' "$img" | tr '/:' '__')"
    raw="$IMG_DIR/.${safe}.tar"
    compressed="$out.tmp.$BASHPID"
    rm -f "$raw" "$compressed"
    if ! docker buildx bake \
        --builder "$BUILDER_NAME" \
        --file "$bake_file" \
        --progress plain \
        --set "$target.output=type=docker,dest=$raw" \
        --set "$target.cache-from=type=local,src=$BUILDX_CACHE_DIR" \
        --set "$target.cache-to=type=local,dest=$BUILDX_CACHE_DIR,mode=max" \
        "$target"; then
        rm -f "$raw" "$compressed"
        return 1
    fi
    if [ ! -s "$raw" ] || [ "$(stat -c%s "$raw")" -lt 1024 ]; then
        echo "FAIL (buildx tar too small)"
        rm -f "$raw" "$compressed"
        return 1
    fi
    # Layer blobs 已經是壓縮格式;外層 gzip 只為相容既有 bundle / checksum / runbook。
    if ! gzip -c "$raw" > "$compressed"; then
        echo "FAIL (gzip)"
        rm -f "$raw" "$compressed"
        return 1
    fi
    mv -f "$compressed" "$out"
    rm -f "$raw"
    return 0
}

if [ "$SKIP_BUILD" = "1" ]; then
    echo "  SKIP_BUILD=1: reuse existing built-image tarballs in $IMG_DIR"
    SKIP_BUILD_MISSING=()
    for img in "${BUILT_IMAGES[@]}"; do
        safe="$(printf '%s' "$img" | tr '/:' '__')"
        out="$IMG_DIR/$safe.tar.gz"
        if [ -f "$out" ]; then
            BUILT_TAR_PATH["$img"]="$out"
            echo "    ✓ reuse $safe.tar.gz ($img)"
        else
            echo "    ✗ missing reusable tar: $safe.tar.gz ($img)"
            SKIP_BUILD_MISSING+=("$img")
        fi
    done
    [ ${#SKIP_BUILD_MISSING[@]} -eq 0 ] \
        || die "SKIP_BUILD=1 但 01-images/ 缺少 built image tar:${SKIP_BUILD_MISSING[*]}"
else
    echo "▶ [1/5] Building ${#BUILT_IMAGES[@]} unique image(s) with isolated buildx tar output..."
    ensure_buildx_builder
    BUILD_FAIL=()
    for line in "${BAKE_TARGET_MAP[@]}"; do
        IFS=$'\t' read -r target img <<< "$line"
        safe="$(printf '%s' "$img" | tr '/:' '__')"
        out="$IMG_DIR/$safe.tar.gz"
        echo -n "  buildx $img (target $target) → 01-images/$safe.tar.gz ... "
        if buildx_export_one "$img" "$target" "$out"; then
            BUILT_TAR_PATH["$img"]="$out"
            echo "OK ($(du -h "$out" | cut -f1))"
        else
            echo "FAIL"
            BUILD_FAIL+=("$img")
        fi
    done
    if [ ${#BUILD_FAIL[@]} -gt 0 ]; then
        echo "✗ buildx tar export failed for: ${BUILD_FAIL[*]}" >&2
        rm -f "$BAKE_FILE"
        exit 1
    fi
    echo "✓ Built ${#BUILT_IMAGES[@]} unique image tar(s)."
fi
echo

# ── Phase 2: built tar ready;缺的上游 image 可 pull,否則失敗 ──────────
echo "▶ [2/5] Ensuring every derived image has a local source..."
MISSING=()
for img in "${IMAGES[@]}"; do
    local_ref="$(tag_only_image_ref "$img")"
    if [ -n "${BUILT_IMAGE_SET[$img]+x}" ]; then
        tar_path="${BUILT_TAR_PATH[$img]:-}"
        if [ -n "$tar_path" ] && [ -s "$tar_path" ]; then
            echo "  ✓ $img (buildx tar)"
        else
            echo "  ✗ MISSING (buildx tar): $img"
            MISSING+=("$img")
        fi
        continue
    fi
    if docker image inspect "$img" >/dev/null 2>&1; then
        echo "  ✓ $img"
        continue
    fi
    if [ "$local_ref" != "$img" ] && docker image inspect "$local_ref" >/dev/null 2>&1; then
        echo "  ✓ $img (tag-only local source: $local_ref; RepoDigest absent after docker load)"
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
    echo "  Fix: rebuild/reuse built tar (SKIP_BUILD=0/1) or pull upstream, then re-run."
    echo "============================================================"
    exit 1
fi
echo "✓ All ${#IMAGES[@]} images present."
echo

# ── Docling image (optional): same five-stage path, not in platform profile ──
# WITH_DOCLING_IMAGE=1 才 bake。故意不把 docling 加進 IMAGES /
# COMPOSE_PROFILE_ARGS / intranet-image-overrides：那些會變成平台主機
# up 的有效組態。映像 tar 仍寫進 01-images/（checksum glob 會涵蓋），
# 但不寫進 01-compose-images.files.txt，所以 INTRANET-LOAD.sh 不會在
# 平台主機 docker load 它。
if [ "$WITH_DOCLING_IMAGE" = "1" ]; then
    echo "▶ [1/5] Deriving docling Bake target (bake-only --profile docling-local; platform COMPOSE_PROFILE_ARGS unchanged)..."
    DOCLING_BAKE_FILE="$(mktemp --suffix=.json)"
    if ! docker compose --env-file "$COMPOSE_ENV_FILE" -p "$COMPOSE_PROJECT_NAME" \
        "${COMPOSE_FILES[@]}" --profile docling-local build --print > "$DOCLING_BAKE_FILE"; then
        rm -f "$DOCLING_BAKE_FILE"
        die "docling docker compose build --print 失敗"
    fi
    DOCLING_TARGET_LINE="$(python3 - "$DOCLING_BAKE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
targets = document.get("target") or {}
found = []
for name, target in targets.items():
    if not isinstance(target, dict):
        continue
    tags = target.get("tags") or []
    tagged = any("docling-service" in str(tag) for tag in tags)
    if name == "docling" or tagged:
        if not isinstance(tags, list) or len(tags) != 1 or not isinstance(tags[0], str) or not tags[0]:
            raise SystemExit(f"docling bake target 必須有一個 image tag:{name}")
        if "${" in json.dumps(target, ensure_ascii=False):
            raise SystemExit(f"docling bake target 仍有未解析的插值:{name}")
        found.append((name, tags[0]))
if len(found) != 1:
    raise SystemExit(f"expected exactly one docling bake target, got {found!r}")
print(f"{found[0][0]}\t{found[0][1]}")
PY
)"
    [ -n "$DOCLING_TARGET_LINE" ] || die "Bake definition 沒有 docling target"
    IFS=$'\t' read -r DOCLING_TARGET DOCLING_IMAGE <<< "$DOCLING_TARGET_LINE"
    [ -n "$DOCLING_TARGET" ] && [ -n "$DOCLING_IMAGE" ] \
        || die "無法解析 docling bake target"
    echo "  ✓ docling bake target: $DOCLING_TARGET → $DOCLING_IMAGE"

    docling_safe="$(printf '%s' "$DOCLING_IMAGE" | tr '/:' '__')"
    docling_out="$IMG_DIR/$docling_safe.tar.gz"
    if [ "$SKIP_BUILD" = "1" ]; then
        [ -f "$docling_out" ] \
            || die "SKIP_BUILD=1 但 01-images/ 缺少 docling image tar:$docling_safe.tar.gz ($DOCLING_IMAGE)"
        echo "  SKIP_BUILD=1: reuse existing $docling_safe.tar.gz ($DOCLING_IMAGE)"
        BUILT_TAR_PATH["$DOCLING_IMAGE"]="$docling_out"
    else
        echo "▶ [1/5] Building docling with isolated buildx tar output..."
        ensure_buildx_builder
        echo -n "  buildx $DOCLING_IMAGE (target $DOCLING_TARGET) → 01-images/$docling_safe.tar.gz ... "
        if buildx_export_one "$DOCLING_IMAGE" "$DOCLING_TARGET" "$docling_out" "$DOCLING_BAKE_FILE"; then
            BUILT_TAR_PATH["$DOCLING_IMAGE"]="$docling_out"
            echo "OK ($(du -h "$docling_out" | cut -f1))"
        else
            echo "FAIL"
            rm -f "$DOCLING_BAKE_FILE"
            die "buildx tar export failed for docling:$DOCLING_IMAGE"
        fi
    fi
    [ -s "${BUILT_TAR_PATH[$DOCLING_IMAGE]}" ] \
        || die "docling buildx tar missing:$docling_out"
    BUILT_IMAGE_SET["$DOCLING_IMAGE"]=1
    BUILT_IMAGES+=("$DOCLING_IMAGE")
    printf '%s\t%s\n' "$docling_safe.tar.gz" "$DOCLING_IMAGE" \
        > "$OUTPUT_DIR/06-docling-image.files.txt"
    printf '%s\n' "$DOCLING_IMAGE" > "$OUTPUT_DIR/06-docling-image.images.txt"
    echo "  ✓ docling tar queued for scan + load-verify (not added to platform image list)"
    echo
fi

# ── Phase 2b: 建後雜物掃描(髒映像不准變成交付品)────────────────────────
# 2026-08-06 驗收在 08-03 那包交付的 csp 映像裡撈出測試用 RSA 私鑰與 25MB 開發期
# 日誌;補了 .dockerignore 之後**重建**的映像裡還是有真實使用者附件與 .pytest_cache。
# 閘門設在這裡而不是 build 之後隨便一個地方:這是「本機髒映像」變成「交付品」的
# 那一步,過了這一步就出門了。
#
# 只掃**本專案 build 出來的**映像(有 build: 的服務),不掃 pg/redis/nginx
# 這些上游映像 —— 那些映像的內容不是我們的 build context 決定的,而且實測就會紅:
# nginx:alpine 有 etc/ssl/cert.pem 與 var/log/nginx/*.log、redis:7-alpine 有
# etc/ssl/cert.pem(2026-08-06 實測)。把不歸我們管、也修不動的東西擋在閘門上,
# 只會逼人去亂加白名單或整段跳過,那時這個閘門就等於不存在了。
echo "▶ [2b/5] Scanning locally built images for baked runtime artifacts..."
SCAN_SCRIPT="${SCAN_SCRIPT:-$REPO_ROOT/infra/deployment/scripts/scan-image-artifacts.sh}"
[ -f "$SCAN_SCRIPT" ] || die "找不到掃描腳本:$SCAN_SCRIPT"

# 進 bundle 的貨**全部**要過這一關,不是只有 compose 那批。
# MODEL_IMAGES 是 WITH_MODELS=1 時 Phase 4 會 save 的六張(其中 embedding-proxy、
# anila-flux-agent 是本專案自建的),原本完全不經過掃描 —— 那等於留了一條
# 「換個旗標就能把髒映像送出門」的路。定義寫在這裡當單一真相來源,Phase 4 直接用。
# 掃在 save **之前**:一發現髒就停,不要先寫了 1.5GB 的 tar 再說。
MODEL_IMAGES=(
    tensorrt-llm-hf:1.3.0rc10
    vllm-gemma4:latest
    tritonserver:25.04-nv-embed-v2
    embedding-proxy:migration
    flux2-dev:bf16
    anila-flux-agent:latest
)

SCAN_INPUTS=()
for img in "${BUILT_IMAGES[@]}"; do
    SCAN_INPUTS+=("${BUILT_TAR_PATH[$img]}")
done
if [ "${WITH_MODELS:-0}" = "1" ]; then
    echo "  (WITH_MODELS=1 → 另外 ${#MODEL_IMAGES[@]} 張 model image 也一起掃;這幾張很大,會花時間)"
    SCAN_INPUTS+=("${MODEL_IMAGES[@]}")
fi

# 掃描輸出留一份,除了給人看,也用來檢查「有沒有哪張其實一個檔都沒掃到」。
SCAN_LOG="$(mktemp)"
SCAN_RC=0
bash "$SCAN_SCRIPT" "${SCAN_INPUTS[@]}" 2>&1 | tee "$SCAN_LOG" || SCAN_RC=$?
# 掃描器必須為每個輸入印出完整 SUMMARY。特別要求 content_paths= 這個
# contract key,否則 scanner 變成只報「✓ 乾淨」的 vacuous-green 也要拒絕。
SUMMARY_COUNT="$(grep -Ec '^  SCAN-SUMMARY image=.* paths=[0-9]+ content_paths=[0-9]+ violations=[0-9]+( layers=[0-9]+)?$' "$SCAN_LOG" || true)"
if [ "$SUMMARY_COUNT" -ne "${#SCAN_INPUTS[@]}" ]; then
    rm -f "$SCAN_LOG"
    die "scanner 沒有為每個輸入回報含 content_paths= 的 SCAN-SUMMARY($SUMMARY_COUNT/${#SCAN_INPUTS[@]})— 當成掃描失敗,拒絕匯出"
fi
TAR_SUMMARY_COUNT="$(grep -Ec '^  SCAN-SUMMARY image=.* paths=[0-9]+ content_paths=[0-9]+ violations=[0-9]+ layers=[0-9]+$' "$SCAN_LOG" || true)"
if [ "$TAR_SUMMARY_COUNT" -ne "${#BUILT_IMAGES[@]}" ]; then
    rm -f "$SCAN_LOG"
    die "built image 沒有全部走 tar-mode SUMMARY($TAR_SUMMARY_COUNT/${#BUILT_IMAGES[@]})— 拒絕匯出"
fi
# 掃到 0 個檔**不是乾淨**,是掃描沒真的看到東西。掃描器自己也會擋(exit 2),
# 這裡再攔一次:閘門不該有「看起來綠的」這種狀態。
if grep -q 'SCAN-SUMMARY .* content_paths=0 ' "$SCAN_LOG"; then
    rm -f "$SCAN_LOG"
    die "有映像回報 content_paths=0(扣掉 docker 容器骨架之後什麼都沒有)— 當成掃描失敗,拒絕匯出"
fi
rm -f "$SCAN_LOG"
if [ "$SCAN_RC" -eq 1 ]; then
    echo
    echo "============================================================"
    echo "✗ REFUSING TO EXPORT — 上面列出的映像烘進了不該出門的執行期產物。"
    echo "  (私鑰 / 日誌 / secrets/ / 使用者附件 / 測試快取)"
    echo
    echo "  沒有跳過這個閘門的旗標,這是刻意的:一包交付品出了門就收不回來。"
    echo "  自救路徑(照順序試):"
    echo "    1. 修 .dockerignore 或 Dockerfile 的 COPY,重 build,再跑一次"
    echo "    2. 成品真的需要它、而且它是公開資訊 → 在掃描腳本的 ALLOWLIST"
    echo "       補一條並寫清楚理由:$SCAN_SCRIPT"
    echo "============================================================"
    exit 1
elif [ "$SCAN_RC" -ne 0 ]; then
    die "映像掃描本身失敗(exit $SCAN_RC)— 在確認掃描能跑之前不匯出"
fi
echo

# ── Phase 2c: load-verify gate──────────────────────────────────────────
# buildx type=docker 已經把 built image 直接寫成 docker-load archive;這一關
# 故意真的 load 一次,確認 archive 沒截斷、manifest 可被 daemon 接受、而且
# expected tag 存在。上游 image 不在這裡 load,仍由 Phase 3 的 docker save 交付。
load_verify_one() {
    # $1=image  $2=tar.gz  → 0/1
    local img="$1" archive="$2" load_err
    if ! load_err="$(gzip -t "$archive" 2>&1)"; then
        echo "FAIL (gzip integrity: ${load_err:-truncated or corrupt archive})"
        return 1
    fi
    load_err="$(mktemp)"
    if ! docker load < "$archive" 2>"$load_err"; then
        echo "FAIL (docker load: $(tr '\n' ' ' <"$load_err"))"
        rm -f "$load_err"
        return 1
    fi
    rm -f "$load_err"
    if ! docker image inspect "$img" >/dev/null 2>&1; then
        echo "FAIL (expected tag missing after docker load: $img)"
        return 1
    fi
    echo "OK (loaded tag verified; kept; no Docker cleanup)"
    return 0
}

echo "▶ [2c/5] Load-verifying built image tars..."
LOAD_VERIFY_FAIL=()
for img in "${BUILT_IMAGES[@]}"; do
    echo -n "  load-verify $img ← $(basename "${BUILT_TAR_PATH[$img]}") ... "
    if load_verify_one "$img" "${BUILT_TAR_PATH[$img]}"; then
        :
    else
        LOAD_VERIFY_FAIL+=("$img")
    fi
done
if [ ${#LOAD_VERIFY_FAIL[@]} -gt 0 ]; then
    echo
    echo "============================================================"
    echo "✗ REFUSING TO EXPORT — built tar load-verify failed:"
    printf '    - %s\n' "${LOAD_VERIFY_FAIL[@]}"
    echo "  A truncated/corrupt tar or missing expected tag is not deliverable."
    echo "============================================================"
    rm -f "$BAKE_FILE" ${DOCLING_BAKE_FILE:+"$DOCLING_BAKE_FILE"}
    exit 1
fi
rm -f "$BAKE_FILE" ${DOCLING_BAKE_FILE:+"$DOCLING_BAKE_FILE"}
echo "✓ All ${#BUILT_IMAGES[@]} built image tar(s) load-verified."
echo

# ── Phase 3: 逐張封裝(一 image 一檔;點名失敗;可續傳)────────────────────
echo "▶ [3/5] Packaging compose images → 01-images/*.tar.gz ..."
mkdir -p "$IMG_DIR"
{
    printf '%s\n' "${IMAGES[@]}"
} > "$OUTPUT_DIR/01-compose-images.images.txt"

# 只有 upstream image 走 docker save; built image 的 tar 在 Phase 1 已由 buildx
# 直接產出。兩條路都維持相同 safe filename / gzip / files.txt contract。
save_one_image() {
    # $1=image  $2=out.tar.gz  → 0/1
    local img="$1" out="$2" err raw save_ref
    save_ref="$(tag_only_image_ref "$img")"
    if [ "$save_ref" = "$img" ] || ! docker image inspect "$save_ref" >/dev/null 2>&1; then
        save_ref="$img"
    fi
    err="$(mktemp)"
    raw="$(mktemp --suffix=.tar)"
    if ! docker save "$save_ref" -o "$raw" 2>"$err"; then
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

PACKAGING_FAIL=()
: > "$OUTPUT_DIR/01-compose-images.files.txt"
for img in "${IMAGES[@]}"; do
    safe="$(printf '%s' "$img" | tr '/:' '__')"
    out="$IMG_DIR/$safe.tar.gz"
    if [ -n "${BUILT_IMAGE_SET[$img]+x}" ]; then
        echo "  buildx tar $img → 01-images/$safe.tar.gz ... OK ($(du -h "$out" | cut -f1))"
        echo "$safe.tar.gz	$img" >> "$OUTPUT_DIR/01-compose-images.files.txt"
        continue
    fi
    echo -n "  docker save $img → 01-images/$safe.tar.gz ... "
    if save_one_image "$img" "$out"; then
        echo "$safe.tar.gz	$img" >> "$OUTPUT_DIR/01-compose-images.files.txt"
    else
        PACKAGING_FAIL+=("$img")
    fi
done

if [ ${#PACKAGING_FAIL[@]} -gt 0 ]; then
    echo
    echo "============================================================"
    echo "✗ REFUSING TO EXPORT — upstream docker save failed for:"
    for img in "${PACKAGING_FAIL[@]}"; do
        echo "    - $img"
    done
    echo
    echo "  Built images already passed buildx tar load-verify; only upstream images use docker save."
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
    # 清單在 Phase 2b 定義(單一真相來源);這幾張已經在那裡跟其他映像
    # **一起掃過**了,掃不過的話根本走不到這裡。
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

# GPU-host four-piece tar. Tests parse AND execute this function (same
# principle as test_dockerfile_cmd.py reading the Dockerfile CMD).
pack_docling_gpu_host_tar() {
    echo "  • 06-docling-gpu-host.tar (standalone yml + .env.example + README)"
    local docling_src="$REPO_ROOT/services/docling-service"
    local gpu_host_dir="$OUTPUT_DIR/06-docling-gpu-host"
    local gpu_file
    mkdir -p "$gpu_host_dir"
    for gpu_file in docker-compose.standalone.yml .env.example README.md; do
        [ -f "$docling_src/$gpu_file" ] \
            || die "GPU-host four-piece source missing:$docling_src/$gpu_file"
        cp "$docling_src/$gpu_file" "$gpu_host_dir/$gpu_file"
    done
    tar -cf "$OUTPUT_DIR/06-docling-gpu-host.tar" \
        -C "$gpu_host_dir" \
        docker-compose.standalone.yml .env.example README.md
    [ -s "$OUTPUT_DIR/06-docling-gpu-host.tar" ] \
        || die "06-docling-gpu-host.tar is empty"
    [ -f "$OUTPUT_DIR/05-weights-docling.tar" ] \
        || die "WITH_DOCLING_IMAGE=1 missing 05-weights-docling.tar (four-piece set)"
    [ -n "${DOCLING_IMAGE:-}" ] && [ -f "$OUTPUT_DIR/06-docling-image.files.txt" ] \
        || die "WITH_DOCLING_IMAGE=1 missing docling image pointer"
    # Staging dir is not a checksummed artifact; drop it so the tar is the only copy.
    rm -rf "$gpu_host_dir"
    echo "    ✓ GPU-host four-piece: image tar + 05-weights-docling.tar + standalone yml + .env.example"
}

if [ "$WITH_DOCLING_WEIGHTS" = "1" ]; then
    [ -n "$DOCLING_WEIGHTS_DIR" ] \
        || die "WITH_DOCLING_WEIGHTS=1 requires DOCLING_WEIGHTS_DIR"
    [ -d "$DOCLING_WEIGHTS_DIR" ] \
        || die "Docling weights directory does not exist: $DOCLING_WEIGHTS_DIR"
    [ -s "$DOCLING_WEIGHTS_DIR/DOCLING-WEIGHTS-MANIFEST.txt" ] \
        || die "Docling weights manifest missing: $DOCLING_WEIGHTS_DIR/DOCLING-WEIGHTS-MANIFEST.txt"
    docling_weight_file="$(find "$DOCLING_WEIGHTS_DIR" -type f -print -quit)"
    [ -n "$docling_weight_file" ] \
        || die "Docling weights directory is empty: $DOCLING_WEIGHTS_DIR"
    echo "  • 05-weights-docling.tar (來源 $DOCLING_WEIGHTS_DIR)"
    tar -cf "$OUTPUT_DIR/05-weights-docling.tar" \
        -C "$DOCLING_WEIGHTS_DIR" .
    [ -s "$OUTPUT_DIR/05-weights-docling.tar" ] \
        || die "Docling weights archive is empty"
    echo "    ✓ Docling artifacts ($(du -sh "$DOCLING_WEIGHTS_DIR" | cut -f1))"
else
    echo "  • 05-weights-docling.tar — skipped (WITH_DOCLING_WEIGHTS=0)"
fi

if [ "$WITH_DOCLING_IMAGE" = "1" ]; then
    pack_docling_gpu_host_tar
else
    echo "  • 06-docling-gpu-host.tar — skipped (WITH_DOCLING_IMAGE=0)"
fi
echo

# built image 的 metadata 必須從它交付的 tar 讀,不能因 load-verify 後 tag 被
# 保留/移除而回頭依賴 daemon state。LayerSources 有 uncompressed layer size;
# 沒有時退回 tar member size,仍然是 archive 自身的資料。
metadata_from_tar() {
    local archive="$1" expected_image="$2"
    python3 - "$archive" "$expected_image" <<'PY'
import gzip
import json
import os
import sys
import tarfile

archive, expected = sys.argv[1:]
with tarfile.open(fileobj=gzip.GzipFile(archive, "rb"), mode="r:") as bundle:
    manifest = json.load(bundle.extractfile(bundle.getmember("manifest.json")))
    if not isinstance(manifest, list) or not manifest:
        raise SystemExit("manifest.json must be a non-empty array")
    records = [entry for entry in manifest if expected in (entry.get("RepoTags") or [])]
    if not records:
        if len(manifest) != 1:
            raise SystemExit(f"tar has no manifest entry for {expected}")
        records = manifest
    entry = records[0]
    config_ref = entry.get("Config")
    if not isinstance(config_ref, str) or not config_ref:
        raise SystemExit(f"tar manifest has no Config for {expected}")
    config_member = bundle.getmember(config_ref)
    config = json.load(bundle.extractfile(config_member))
    if not isinstance(config, dict):
        raise SystemExit(f"config blob is not an object for {expected}")

    config_name = os.path.basename(config_ref)
    if config_name.endswith(".json"):
        config_name = config_name[:-5]
    if not config_name.startswith("sha256:"):
        config_name = "sha256:" + config_name

    layer_sources = entry.get("LayerSources") or {}
    size = 0
    for layer_ref in entry.get("Layers") or []:
        if not isinstance(layer_ref, str):
            raise SystemExit(f"invalid layer reference for {expected}")
        digest = "sha256:" + os.path.basename(layer_ref)
        source = layer_sources.get(digest) or layer_sources.get(os.path.basename(layer_ref))
        if isinstance(source, dict) and isinstance(source.get("size"), int):
            size += source["size"]
        else:
            size += bundle.getmember(layer_ref).size

    repo_digests = entry.get("RepoDigests") or []
    if not isinstance(repo_digests, list):
        repo_digests = []
    print(f"{config_name}\t{size}\t{json.dumps(repo_digests, separators=( ',', ':' ))}")
PY
}

# Collect checksum inputs. cwd must be OUTPUT_DIR.
# Tests parse AND execute this function (same principle as
# test_dockerfile_cmd.py reading the Dockerfile CMD). Do not rewrite the
# glob in tests — the glob list here is the single source.
bundle_checksum_files() {
    shopt -s nullglob
    local f
    for f in 01-images/*.tar.gz 04-models.tar.gz 05-weights-*.tar 06-docling-gpu-host.tar; do
        if [ -f "$f" ]; then
            printf '%s\n' "$f"
        fi
    done
    return 0
}

# Files THIS run declared it would produce. Same cwd as bundle_checksum_files.
# Source of truth: this run's flags + pointer files this run wrote.
# Tests parse AND execute this function.
bundle_declared_checksum_files() {
    local file img w
    if [ -f 01-compose-images.files.txt ]; then
        while IFS=$'\t' read -r file img; do
            [ -n "$file" ] || continue
            printf '%s\n' "01-images/$file"
        done < 01-compose-images.files.txt
    fi
    if [ "${WITH_MODELS:-0}" = "1" ]; then
        printf '%s\n' "04-models.tar.gz"
    fi
    if [ "${WITH_WEIGHTS:-0}" = "1" ]; then
        for w in ${WEIGHTS_LIST:-}; do
            printf '%s\n' "05-weights-${w}.tar"
        done
    fi
    if [ "${WITH_DOCLING_WEIGHTS:-0}" = "1" ]; then
        printf '%s\n' "05-weights-docling.tar"
    fi
    if [ "${WITH_DOCLING_IMAGE:-0}" = "1" ]; then
        printf '%s\n' "06-docling-gpu-host.tar"
        if [ -f 06-docling-image.files.txt ]; then
            while IFS=$'\t' read -r file img; do
                [ -n "$file" ] || continue
                printf '%s\n' "01-images/$file"
            done < 06-docling-image.files.txt
        fi
    fi
}

# Every file the checksum glob picks up must be traceable to something
# THIS run declared it would produce. Unexpected file → die, naming it.
# Declared file the glob missed → die (produced tar silently off the chain).
# Tests parse AND execute this function. MANIFEST / loader provenance
# statements use the same WITH_DOCLING_* flags this predicate reads.
# Does not delete leftovers.
assert_checksum_matches_declared() {
    local f leftover
    local -a actual declared unexpected missing stale
    local -A actual_set declared_set
    mapfile -t actual < <(bundle_checksum_files)
    mapfile -t declared < <(bundle_declared_checksum_files)
    actual_set=()
    declared_set=()
    unexpected=()
    missing=()
    stale=()
    for f in "${actual[@]}"; do
        [ -n "$f" ] || continue
        actual_set["$f"]=1
    done
    for f in "${declared[@]}"; do
        [ -n "$f" ] || continue
        declared_set["$f"]=1
    done
    for f in "${actual[@]}"; do
        [ -n "$f" ] || continue
        if [ -z "${declared_set[$f]+x}" ]; then
            unexpected+=("$f")
        fi
    done
    for f in "${declared[@]}"; do
        [ -n "$f" ] || continue
        if [ -z "${actual_set[$f]+x}" ]; then
            missing+=("$f")
        fi
    done
    if [ ${#unexpected[@]} -gt 0 ]; then
        echo "✗ checksum glob picked up file(s) this run did not declare (WITH_DOCLING_IMAGE=${WITH_DOCLING_IMAGE:-0} WITH_DOCLING_WEIGHTS=${WITH_DOCLING_WEIGHTS:-0} WITH_MODELS=${WITH_MODELS:-0} WITH_WEIGHTS=${WITH_WEIGHTS:-0}):" >&2
        printf '    %s\n' "${unexpected[@]}" >&2
        echo "  Leftovers from a previous run, or a tar that landed outside this run's output. Remove them yourself or re-run with the matching flag. This script will not delete them." >&2
        exit 1
    fi
    if [ ${#missing[@]} -gt 0 ]; then
        echo "✗ this run declared file(s) that the checksum glob did not pick up:" >&2
        printf '    %s\n' "${missing[@]}" >&2
        echo "  A produced tar that is not in CHECKSUMS.sha256 would pass sha256sum -c on the receiving end while silently omitting the file. Fix the write path or the glob in bundle_checksum_files." >&2
        exit 1
    fi
    # Pointer / staging leftovers are not in the checksum glob but still travel
    # with the bundle. Fail-loud; do not delete.
    if [ "${WITH_DOCLING_IMAGE:-0}" != "1" ]; then
        for leftover in 06-docling-image.files.txt 06-docling-image.images.txt 06-docling-gpu-host; do
            if [ -e "$leftover" ]; then
                stale+=("$leftover")
            fi
        done
        if [ ${#stale[@]} -gt 0 ]; then
            echo "✗ WITH_DOCLING_IMAGE=${WITH_DOCLING_IMAGE:-0} but leftover 06-docling-* still in OUTPUT_DIR:" >&2
            printf '    %s\n' "${stale[@]}" >&2
            echo "  Remove them yourself or re-run with WITH_DOCLING_IMAGE=1. This script will not delete them." >&2
            exit 1
        fi
    else
        if [ -e 06-docling-gpu-host ]; then
            echo "✗ staging dir 06-docling-gpu-host/ is not a checksummed artifact; pack_docling_gpu_host_tar must remove it after writing the tar." >&2
            exit 1
        fi
        if [ ! -f 06-docling-image.files.txt ]; then
            echo "✗ WITH_DOCLING_IMAGE=1 declared a docling image but 06-docling-image.files.txt is missing." >&2
            exit 1
        fi
    fi
}

# ── Phase 5: MANIFEST + INTRANET-LOAD.sh ────────────────────────────────
echo "▶ [4/5] Writing MANIFEST.txt + INTRANET-LOAD.sh..."

(
    cd "$OUTPUT_DIR"
    # 完整性鏈要涵蓋「實際產出的每一個 tar」。字面 04-models.tar.gz 不是 glob,
    # nullglob 蓋不到它——它是選配,不存在時若直接塞給 sha256sum 會整行失敗。
    # 舊寫法用 `||` 退到「只算映像」,把 05-weights-*.tar 掉出鏈還回一個綠燈,
    # 而載入端訊息卻宣稱它們受 CHECKSUMS 保護(=假保護)。所以:只對實際存在
    # 的檔案算 hex,不退到較弱的命令。
    assert_checksum_matches_declared
    mapfile -t CHECKSUM_FILES < <(bundle_checksum_files)
    if [ ${#CHECKSUM_FILES[@]} -eq 0 ]; then
        echo "✗ 沒有可計算 checksum 的交付檔(01-images 至少應有一份)" >&2
        exit 1
    fi
    sha256sum "${CHECKSUM_FILES[@]}" > CHECKSUMS.sha256
)

{
    echo "ANILA Platform — Intranet Image Bundle"
    echo "Built at: $(date -Iseconds)"
    echo "Built by: $(whoami)@$(hostname)"
    echo "Repo:     $REPO_ROOT"
    echo "Branch:   $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'n/a')"
    echo "Commit:   $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'n/a')"
    echo "Ref:      $(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo 'n/a')"
    echo "Project:  $COMPOSE_PROJECT_NAME"
    echo "INCLUDE_ASR: $INCLUDE_ASR"
    echo "WITH_DOCLING_IMAGE: $WITH_DOCLING_IMAGE"
    echo "WITH_DOCLING_WEIGHTS: $WITH_DOCLING_WEIGHTS"
    echo "Compose:  ${COMPOSE_FILES[*]} ${COMPOSE_PROFILE_ARGS[*]:-}"
    echo
    echo "── Service → image (from compose config) ──────────────"
    echo "$SERVICE_IMAGE_MAP"
    echo
    echo "── Images in 01-images/*.tar.gz ───────────────────────"
    while IFS=$'\t' read -r file img; do
        [ -n "$img" ] || continue
        if [ -n "${BUILT_IMAGE_SET[$img]+x}" ]; then
            meta="$(metadata_from_tar "$IMG_DIR/$file" "$img")" \
                || die "無法從 built tar 讀取 metadata:$file ($img)"
            IFS=$'\t' read -r id bytes digests <<< "$meta"
        else
            inspect_ref="$img"
            if ! meta="$(docker image inspect "$inspect_ref" --format '{{.Id}} {{.Size}} {{json .RepoDigests}}' 2>/dev/null)"; then
                inspect_ref="$(tag_only_image_ref "$img")"
                meta="$(docker image inspect "$inspect_ref" --format '{{.Id}} {{.Size}} {{json .RepoDigests}}' 2>/dev/null || echo '? ? []')"
            fi
            id="$(awk '{print $1}' <<<"$meta")"
            bytes="$(awk '{print $2}' <<<"$meta")"
            digests="$(awk '{$1="";$2=""; sub(/^  /,""); print}' <<<"$meta")"
        fi
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
    if [ "$WITH_DOCLING_IMAGE" = "1" ]; then
        echo
        echo "── Docling image (GPU host; not loaded on platform) ──"
        while IFS=$'\t' read -r file img; do
            [ -n "$img" ] || continue
            echo "  $img"
            echo "    Archive: 01-images/$file"
            echo "    Load on the GPU host, not on the platform host."
        done < "$OUTPUT_DIR/06-docling-image.files.txt"
        echo
        echo "── GPU-host four-piece set (WITH_DOCLING_IMAGE=$WITH_DOCLING_IMAGE) ────"
        echo "  1. image tar: 01-images/ (see 06-docling-image.files.txt)"
        echo "  2. weights tar: 05-weights-docling.tar (copy, do not move)"
        echo "  3. docker-compose.standalone.yml (inside 06-docling-gpu-host.tar)"
        echo "  4. .env.example (inside 06-docling-gpu-host.tar)"
        echo "  README.md travels in the same tar. Platform up must NOT add --profile docling-local."
    fi
    echo
    echo "── Bundle files ───────────────────────────────────────"
    du -sh "$OUTPUT_DIR" "$IMG_DIR"
    ls -lh "$IMG_DIR"
    ls -lh "$OUTPUT_DIR"/*.{txt,sha256,sh,yml,tar.gz,tar} 2>/dev/null || true
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
# 載入的 image tag 會累積在主機上；本 loader 刻意永遠不執行 Docker cleanup。
# tag verify 的證明邊界：docker image inspect <tag> 只證明 tag 存在，不證明 THIS tar 產生了它；
# 在 virgin air-gap host 上兩者等價，provenance 仍由 CHECKSUMS 承擔。
set -euo pipefail
cd "\$(dirname "\${BASH_SOURCE[0]}")"

# ── Bundle 身分閘:演練包不得不知不覺上正式機 ────────────────────────────
# MANIFEST 的 Ref 是「演練包 vs 出貨包」的機械判準:乾淨 tag=出貨包;
# -dirty / <tag>-N-g<hash>(不在 tag 上)/ 裸 commit hash / 缺欄位=演練包。
if [ ! -f MANIFEST.txt ]; then
    echo "✗ MANIFEST.txt 不存在 — 無法判定包身分(演練包 vs 出貨包);拒絕 load。" >&2
    exit 1
fi
BUNDLE_REF="\$(sed -n 's/^Ref:[[:space:]]*//p' MANIFEST.txt | head -n 1 || true)"
REHEARSAL_BUNDLE=1
case "\$BUNDLE_REF" in
    v*-dirty ) ;;
    v*-[0-9]*-g[0-9a-f]* ) ;;   # git describe 的 <tag>-<N>-g<hash> 形;⚠ 別把正式 tag 取成這個形狀
    v* ) REHEARSAL_BUNDLE=0 ;;
esac
if [ "\$REHEARSAL_BUNDLE" = "1" ]; then
    if [ "\${ALLOW_REHEARSAL_BUNDLE:-0}" = "1" ]; then
        echo "⚠ 此包 Ref='\$BUNDLE_REF' 不是乾淨 tag(演練包)。ALLOW_REHEARSAL_BUNDLE=1 已明確允許載入。" >&2
        echo
    else
        echo "✗ 此包 Ref='\$BUNDLE_REF' 不是乾淨 tag — 這是演練包,不得部署到正式機。" >&2
        echo "  出貨包必須從乾淨 tag 重建(打 tag→重建→重出貨)。演練環境要載入,設 ALLOW_REHEARSAL_BUNDLE=1。" >&2
        exit 1
    fi
fi

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
    if [ "\${ALLOW_NO_CHECKSUMS:-0}" = "1" ]; then
        NO_CHECKSUM_PROTECTION=1
        echo "⚠ CHECKSUMS.sha256 不存在 — 它保護 bundle image/model/weight archives 的 sha256 完整性鏈；ALLOW_NO_CHECKSUMS=1 已明確允許繼續，這次不具備 checksum 保護。" >&2
        echo
    else
        echo "✗ missing CHECKSUMS.sha256 — 它保護 bundle image/model/weight archives 的 sha256 完整性鏈；拒絕 load。若要明確接受無 checksum 保護，請設定 ALLOW_NO_CHECKSUMS=1。" >&2
        exit 1
    fi
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
DOCLING_TAR="05-weights-docling.tar"
shopt -s nullglob
WEIGHT_TARS=(05-weights-*.tar)
if [ \${#WEIGHT_TARS[@]} -gt 0 ]; then
    echo "── Extracting model weights → \$HF_DIR ──"
    mkdir -p "\$HF_DIR"
    for tar in "\${WEIGHT_TARS[@]}"; do
        if [ "\$tar" = "\$DOCLING_TAR" ]; then
            continue
        fi
        echo "▶ \$tar"
        tar -xf "\$tar" -C "\$HF_DIR"
    done
    echo
fi

EOF

# Provenance claims in the generated loader come from THIS run's flags
# (same source of truth as assert_checksum_matches_declared), not from
# leftover files that happen to exist at load time.
if [ "$WITH_DOCLING_WEIGHTS" = "1" ]; then
    cat >> "$OUTPUT_DIR/INTRANET-LOAD.sh" <<'DOCLING_WEIGHTS_LOAD'
if [ -f "$DOCLING_TAR" ]; then
    echo "⚠ $DOCLING_TAR 存在 — docling 權重屬於 GPU 主機,**不在本機解開**。"
    echo "  它仍受 CHECKSUMS.sha256 保護(bundle 完整性鏈)。把它**複製**(不要移動)到"
    echo "  docling-service 跑的那台 GPU 主機,解到 DOCLING_ARTIFACTS_DIR"
    echo "  (compose 的 \${DOCLING_MODEL_HOST_DIR} 掛載目錄);平台主機不跑 docling,"
    echo "  不把權重解在這裡。"
    echo "  ⚠ 用「複製」不是「移動」:本檔名列在 CHECKSUMS.sha256 裡,搬走之後"
    echo "  再跑一次載入腳本,sha256sum -c 會因為「清單有它、檔案不在」而擋死。"
    echo
fi
DOCLING_WEIGHTS_LOAD
fi

if [ "$WITH_DOCLING_IMAGE" = "1" ]; then
    {
        echo 'echo "⚠ docling 映像與 GPU 主機四件套屬於 GPU 主機,**不在本機 docker load / 解開**。"'
        echo 'echo "  平台主機 up **不要**加 --profile docling-local。"'
        echo 'echo "  把下列檔案**複製**(不要移動)到 GPU 主機："'
        while IFS=$'\t' read -r file img; do
            [ -n "$file" ] || continue
            printf 'echo "    - 01-images/%s  (%s)  → 在 GPU 主機 gunzip -c … | docker load"\n' "$file" "$img"
        done < "$OUTPUT_DIR/06-docling-image.files.txt"
        echo 'echo "    - 05-weights-docling.tar  → 解到 DOCLING_MODEL_HOST_DIR"'
        echo 'echo "    - 06-docling-gpu-host.tar  → 含 docker-compose.standalone.yml、.env.example、README.md"'
        echo 'echo'
    } >> "$OUTPUT_DIR/INTRANET-LOAD.sh"
fi

cat >> "$OUTPUT_DIR/INTRANET-LOAD.sh" <<EOF
echo "── Verifying loaded tags (from *.images.txt) ──"
shopt -s nullglob
for list in *.images.txt; do
    case "\$list" in
        06-docling-image.images.txt)
            echo "  skip \$list (GPU-host image; not loaded on this platform host)"
            continue
            ;;
    esac
    while IFS= read -r img; do
        [ -z "\$img" ] && continue
        verify_img="\$img"
        case "\$verify_img" in
            *@sha256:*) verify_img="\${verify_img%@sha256:*}" ;;
        esac
        if docker image inspect "\$verify_img" >/dev/null 2>&1; then
            if [ "\$verify_img" = "\$img" ]; then
                echo "  ✓ \$img"
            else
                echo "  ✓ \$img (tag-only verify: \$verify_img)"
            fi
        else
            echo "  ✗ missing after load: \$img (verified reference: \$verify_img)"
            exit 1
        fi
    done < "\$list"
done
echo
echo "✓ Load complete."
# ⚠ 尾端重印:第一行的警告會被上面幾十行 ✓ 洗掉(#16 正是這樣誕生的)。
if [ "\$REHEARSAL_BUNDLE" = "1" ]; then
    echo "⚠ 再次提醒:本次載入的是演練包(Ref='\$BUNDLE_REF'),不具備出貨資格。" >&2
fi
if [ "\${NO_CHECKSUM_PROTECTION:-0}" = "1" ]; then
    echo "⚠ 再次提醒:本次載入不具備 checksum 完整性保護(ALLOW_NO_CHECKSUMS=1)——載入內容未經 sha256 驗證。" >&2
fi
echo "  下一步見 docs/runbooks/intranet-image-bundle.md"
echo "  起棧時 -p 必須是: $COMPOSE_PROJECT_NAME"
echo "  INCLUDE_ASR 打包值: $INCLUDE_ASR → 預設不開語音;要開才 --profile asr(且映像須已在包內)"
echo "  WITH_DOCLING_IMAGE 打包值: $WITH_DOCLING_IMAGE → 平台 up **不要**加 --profile docling-local"
echo "  起棧命令: docker compose --env-file .env -p $COMPOSE_PROJECT_NAME -f compose.yaml -f intranet-image-overrides.yml ${COMPOSE_PROFILE_ARGS[*]:-} up -d --no-build"
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
