#!/usr/bin/env bash
# build-and-export-for-intranet.sh
# ============================================================================
# 從「有效 compose 組態」衍生要打包的 image 清單 → 確認本機都有 →
# docker save 成 tar.gz,帶進無外網的內網主機 docker load。
#
# ⚠ 清單不再手寫。新增 compose service 會自動進 bundle;漏包只能發生在
#   「本機根本沒有那張 image」,那時腳本會大聲失敗而不是靜默略過。
#
# ⚠ build 之後、save 之前有一道**建後雜物掃描**([2b/5],
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

# ── Phase 2b: 建後雜物掃描(髒映像不准變成交付品)────────────────────────
# 2026-08-06 驗收在 08-03 那包交付的 csp 映像裡撈出測試用 RSA 私鑰與 25MB 開發期
# 日誌;補了 .dockerignore 之後**重建**的映像裡還是有真實使用者附件與 .pytest_cache。
# 閘門設在這裡而不是 build 之後隨便一個地方:這是「本機髒映像」變成「交付品」的
# 那一步,過了這一步就出門了。
#
# 只掃**本專案 build 出來的**映像(有 build: 的服務),不掃 pg/redis/nginx/gitlab
# 這些上游映像 —— 那些映像的內容不是我們的 build context 決定的,而且實測就會紅:
# nginx:alpine 有 etc/ssl/cert.pem 與 var/log/nginx/*.log、redis:7-alpine 有
# etc/ssl/cert.pem(2026-08-06 實測)。把不歸我們管、也修不動的東西擋在閘門上,
# 只會逼人去亂加白名單或整段跳過,那時這個閘門就等於不存在了。
echo "▶ [2b/5] Scanning locally built images for baked runtime artifacts..."
SCAN_SCRIPT="$REPO_ROOT/infra/deployment/scripts/scan-image-artifacts.sh"
[ -f "$SCAN_SCRIPT" ] || die "找不到掃描腳本:$SCAN_SCRIPT"

mapfile -t BUILT_IMAGES < <(
    COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" compose config --format json | python3 -c '
import json, os, sys
project = os.environ["COMPOSE_PROJECT_NAME"]
cfg = json.load(sys.stdin)
out = set()
for name, svc in (cfg.get("services") or {}).items():
    if svc.get("build") is None:
        continue
    out.add(svc.get("image") or f"{project}-{name}")
for img in sorted(out):
    print(img)
'
)
# 衍生不到任何 build 出來的映像 = 上面那段或 compose 組態壞了。靜默略過掃描
# 正好會在「最需要它」的時候發生,所以這裡硬失敗。
[ ${#BUILT_IMAGES[@]} -gt 0 ] || die "衍生不到任何有 build: 的服務映像 — 掃描無法進行,拒絕匯出"

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

SCAN_IMAGES=("${BUILT_IMAGES[@]}")
if [ "${WITH_MODELS:-0}" = "1" ]; then
    echo "  (WITH_MODELS=1 → 另外 ${#MODEL_IMAGES[@]} 張 model image 也一起掃;這幾張很大,會花時間)"
    SCAN_IMAGES+=("${MODEL_IMAGES[@]}")
fi

# 掃描輸出留一份,除了給人看,也用來檢查「有沒有哪張其實一個檔都沒掃到」。
SCAN_LOG="$(mktemp)"
SCAN_RC=0
bash "$SCAN_SCRIPT" "${SCAN_IMAGES[@]}" 2>&1 | tee "$SCAN_LOG" || SCAN_RC=$?
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

# ── Phase 2c: 匯不匯得出去(save-ability)—— 掃描器結構上看不見的那一類 ────
# 為什麼掃描綠了還要再擋一道:掃描器讀的是 `docker export` 的**攤平**檔案系統,
# 交付品卻是 `docker save` 的**每一層**。兩者看到的東西不一樣,而且差異不是理論的
# —— 2026-08-06 實測(alpine:layer A 被本機 IDS 注入且不清、layer B 事後才清):
#   攤平後乾淨 → 掃描器印「✓ 乾淨」;同一張映像 `docker save` 直接失敗
#   open …/merged/run/sisidsdaemon.pid: no such file or directory
# 也就是說**掃描器判乾淨不代表這張映像出得了門**。同一天驗收就是被這個形狀擋下的:
# 同一份 Dockerfile,一次建置匯得出去、下一次匯不出去。
#
# 沒有這道檢查的話,一張匯不出來的映像會一路綠燈走到 Phase 3,在已經寫了好幾 GB
# 之後才炸 —— 這是最貴的失敗時機:人已經走開了,而 OUTPUT_DIR 裡是半包東西。
#
# 只檢 `$BUILT_IMAGES`(compose 裡有 build: 的那批)。範圍邊界寫清楚,不要以為
# 這一關蓋住全部:
#   • pull 進來的上游映像(pg/redis/nginx/…)不檢 —— 這個缺陷來自「在這台主機上
#     跑過 build 容器」,它們沒跑過。真的因別的原因 save 失敗,Phase 3 原本的
#     處理照舊,這一關是加上去的、不是取代。
#   • WITH_MODELS=1 的那六張**也不檢**:其中兩張是本機自建的,原則上會中,但它們
#     動輒數十 GB,為了這一關多讀一遍不划算。它們在 Phase 4 被 save 時一樣會擋。
#
# 檢查方式就是真的 save 一次然後丟掉(不落地、不 gzip;gzip 才是貴的那一半)。
# 代價是把 build 出來的那批多讀一遍;換到的是壞消息出現在**還沒寫任何 bytes 之前**。
echo "▶ [2c/5] Verifying locally built images can actually be saved..."
SAVE_UNABLE=()
for img in "${BUILT_IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || continue   # 缺圖已在 Phase 2 擋過
    echo -n "  save-check $img ... "
    save_err="$(mktemp)"
    if docker save "$img" >/dev/null 2>"$save_err"; then
        echo "OK"
    else
        echo "FAIL"
        echo "    $(tr '\n' ' ' <"$save_err")"
        SAVE_UNABLE+=("$img")
    fi
    rm -f "$save_err"
done

if [ ${#SAVE_UNABLE[@]} -gt 0 ]; then
    if [ "$REBUILD_ON_SAVE_FAIL" = "1" ]; then
        # 使用者已經明講要自動重建重試,那就把處置交給 Phase 3 原本那條路,
        # 不要在這裡先斬 —— 否則 REBUILD_ON_SAVE_FAIL 這個逃生口等於被廢掉。
        echo
        echo "  ⚠ 上面 ${#SAVE_UNABLE[@]} 張映像現在 save 不出來,但 REBUILD_ON_SAVE_FAIL=1,"
        echo "    交給 Phase 3 的重建重試處理。"
    else
        echo
        echo "============================================================"
        echo "✗ REFUSING TO EXPORT — 這些映像掃描是綠的,但 docker save 匯不出去:"
        for img in "${SAVE_UNABLE[@]}"; do
            echo "    - $img"
        done
        echo
        echo "  這台主機上最常見的成因:Symantec DCS 代理(sisidsdaemon)在 build"
        echo "  容器啟動當下注入 /run/sisidsdaemon.pid 與 /var/lib/sdcssagent,"
        echo "  容器結束後又自己刪掉 pid 檔,layer metadata 留下懸空項目。"
        echo "  自救路徑(照順序試):"
        echo "    1. 重 build 那張映像(每次建置各擲一次骰子,重建通常就過了)"
        echo "    2. 根治:在**產生它的那一個 RUN 自己的結尾**加"
        echo "       \`rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid\`。"
        echo "       ⚠ 事後補一層 RUN 清理是**無效**的 —— 攤平後乾淨、掃描器也會"
        echo "       說乾淨,但早一層已經 commit 進去的懸空項目修不回來。"
        echo "    3. 棧可以停的話:停棧再匯出(Fix A),或 REBUILD_ON_SAVE_FAIL=1"
        echo "       走 Phase 3 的自動重建(見 runbook §8)。"
        echo
        echo "  停在這裡是刻意的:此刻 $OUTPUT_DIR 還沒被寫進任何一個 bytes。"
        echo "============================================================"
        exit 1
    fi
fi
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
