#!/usr/bin/env bash
# build-release.sh — 在有網路的開發機打包一版。
# 內網主機不要跑這支。內網只用出貨包裡的 anila-update.sh。
#
#   bash scripts/release/build-release.sh [輸出目錄]
#
# 工作目錄不乾淨就拒絕。版本是 YYYY.MM.DD-N，同一天自動加 1。
# 不要把 .env、secrets/、憑證私鑰、備份打進去。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
source "$SCRIPT_DIR/release-lib.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 映像用隔離的 docker-container buildx builder 直接輸出 tar，不經本機 daemon。
# 這台開發機的賽門鐵克 IDS（sisidsdaemon）會弄壞 daemon 的 overlay 層，
# 對建好的映像 docker save 會失敗，停棧也沒用（2026-08-02 實測、08-13 定案）。
RELEASE_BUILDER="${ANILA_RELEASE_BUILDER:-anila-pkg}"

release_ensure_builder() {
  local driver inspect_out
  if ! docker buildx inspect "$RELEASE_BUILDER" >/dev/null 2>&1; then
    info "建立 buildx builder ${RELEASE_BUILDER}（docker-container）"
    docker buildx create --name "$RELEASE_BUILDER" --driver docker-container >/dev/null \
      || die "無法建立 buildx builder ${RELEASE_BUILDER}"
  fi
  # 先存下來再解析。awk 找到就 exit，pipefail 下 docker 會吃到 SIGPIPE 回 255。
  inspect_out="$(docker buildx inspect "$RELEASE_BUILDER" 2>/dev/null)" || die "讀不到 builder ${RELEASE_BUILDER}"
  driver="$(awk -F': *' '/^Driver:/ { print $2; exit }' <<< "$inspect_out")"
  [[ "$driver" == "docker-container" ]] \
    || die "builder ${RELEASE_BUILDER} 不是 docker-container（${driver:-未知}），拒絕用它打包"
  docker buildx inspect --bootstrap "$RELEASE_BUILDER" >/dev/null \
    || die "builder ${RELEASE_BUILDER} 啟動失敗"
}

# 印出「設定檔雜湊 manifest 雜湊」。傳統儲存（overlay2）載入後的映像 ID 是前者，
# containerd 儲存（Docker 29 新裝的預設）是後者。兩個都記，主機對上其一即可。
release_archive_digests() {
  python3 - "$1" <<'PY'
import gzip, json, sys, tarfile
with gzip.open(sys.argv[1], "rb") as gz, tarfile.open(fileobj=gz, mode="r:") as tf:
    names = {m.name.lstrip("./"): m for m in tf.getmembers()}
    def load(name):
        m = names.get(name)
        if m is None:
            raise SystemExit(f"封存缺少 {name}：{sys.argv[1]}")
        return json.load(tf.extractfile(m))
    configs = {e["Config"].rsplit("/", 1)[-1] for e in load("manifest.json")}
    manifests = {m["digest"] for m in load("index.json").get("manifests", [])}
if len(configs) != 1 or len(manifests) != 1:
    raise SystemExit(f"封存裡不是剛好一張映像：{sys.argv[1]}")
cfg = configs.pop()
cfg = cfg if cfg.startswith("sha256:") else "sha256:" + cfg
print(cfg, manifests.pop())
PY
}

_release_build_images_impl() {
  local repo="$1" stage="$2" ver="$3" lines="$4" src="$5"
  local override build_env bake_json map scan svc image archive start plain target out raw
  local cfg man got tag
  override="$(mktemp)"
  : > "$lines"
  printf 'services:\n  csp:\n    build:\n      args:\n        ANILA_RELEASE_VERSION: "%s"\n' "$ver" > "$override"
  # 乾淨檢出沒有 .env，compose 在 build 也會先代換變數，必填變數缺值就停。
  # 給一份只有佔位值的 env 檔。這些值只在執行期用，不會進映像。
  build_env="$(mktemp)"
  { grep -ohE '\$\{[A-Z0-9_]+:\?' "$src/compose.yaml" "$src"/infra/compose/*.yml 2>/dev/null || true; } \
    | sed -E 's/^\$\{//; s/:\?$//' | sort -u \
    | while read -r key; do printf '%s=build-placeholder-not-a-secret\n' "$key"; done > "$build_env"
  bake_json="$(mktemp)"
  # 建置上下文是 HEAD 的乾淨檢出，不是工作目錄。被忽略的檔進不了映像。
  docker compose --env-file "$build_env" -f "$src/compose.yaml" -f "$override" build --print \
    csp-db pgbouncer csp-credential-dirs csp ingestion-worker router nginx \
    pptx-renderer anila-studio anilalm anila-ui codeserver n8n asr-gateway > "$bake_json" \
    || die "無法產生建置定義（compose build --print）"
  rm -f "$override" "$build_env"
  map="$(python3 - "$bake_json" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for name, target in (doc.get("target") or {}).items():
    tags = target.get("tags") or []
    if len(tags) != 1:
        raise SystemExit(f"建置目標 {name} 必須剛好一個標籤")
    tag = tags[0]
    if ":" not in tag.rsplit("/", 1)[-1]:
        tag += ":latest"
    print(f"{tag}\t{name}")
PY
)" || die "建置定義無法解析"
  declare -A target_of=()
  while IFS=$'\t' read -r tag target; do
    [[ -n "$tag" ]] && target_of["$tag"]="$target"
  done <<< "$map"
  release_ensure_builder
  info "取得 redis 映像，一併放進出貨包"
  # redis:7-alpine manifest list，2026-09-29。pull 與 images.tsv 釘這筆 digest。
  # 上游映像不是本機建的，照舊 pull 再 save。
  docker pull redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
  declare -A built=()
  while read -r svc image archive start; do
    [[ -n "${built[$archive]:-}" ]] && continue
    built["$archive"]=1
    plain="${image%@sha256:*}"
    out="$stage/images/${archive}.tar.gz"
    tag="anila-bundle/${archive}:${ver}"
    target="${target_of[$plain]:-}"
    if [[ -n "$target" ]]; then
      info "建置 ${plain}（buildx 直接輸出 tar）"
      raw="$stage/images/.${archive}.tar"
      docker buildx bake --builder "$RELEASE_BUILDER" --file "$bake_json" --progress plain \
        --set "${target}.output=type=docker,dest=${raw}" \
        --set "${target}.tags=${tag}" \
        "$target" || die "建置失敗：${plain}"
      [[ -s "$raw" && "$(stat -c %s "$raw")" -ge 1024 ]] || die "建置產出的 tar 不完整：${plain}"
      gzip -c "$raw" > "$out"
      rm -f "$raw"
    else
      [[ "$image" == *@sha256:* ]] || die "${image} 沒有建置定義，也沒有釘 digest"
      docker tag "$image" "$tag"
      docker save "$tag" | gzip -c > "$out"
    fi
  done < <(release_catalog)
  rm -f "$bake_json"
  scan="$src/infra/deployment/scripts/scan-image-artifacts.sh"
  if [[ ! -f "$scan" ]]; then
    scan="$repo/infra/deployment/scripts/scan-image-artifacts.sh"
  fi
  [[ -f "$scan" ]] || die "找不到映像掃描腳本"
  declare -A checked=()
  while read -r svc image archive start; do
    out="$stage/images/${archive}.tar.gz"
    read -r cfg man < <(release_archive_digests "$out") || die "讀不到映像雜湊：${archive}"
    [[ "$cfg" =~ ^sha256:[0-9a-f]+$ && "$man" =~ ^sha256:[0-9a-f]+$ ]] || die "映像雜湊格式不對：${archive}"
    printf 'image %s %s %s images/%s.tar.gz %s\n' "$svc" "$image" "$cfg" "$archive" "$man" >> "$lines"
    [[ -n "${checked[$archive]:-}" ]] && continue
    checked["$archive"]=1
    # 每一張都逐層掃，含上游 redis。後層刪掉的私鑰仍算違規。
    info "逐層掃描封存 images/${archive}.tar.gz"
    bash "$scan" "$out"
    # 真的載入一次，確認載得起來、ID 對得上清單。
    gzip -dc "$out" | docker load >/dev/null || die "封存載不進來：${archive}"
    got="$(docker image inspect --format '{{.Id}}' "anila-bundle/${archive}:${ver}" 2>/dev/null || true)"
    [[ "$got" == "$cfg" || "$got" == "$man" ]] || die "載入後的映像 ID 對不上清單：${archive}"
  done < <(release_catalog)
}

release_build_images() {
  local repo="$1" clean rc
  git -C "$repo" rev-parse --verify HEAD >/dev/null 2>&1 \
    || die "建置映像必須從 git HEAD 的乾淨檢出，不能用工作目錄裡的檔案"
  clean="$(mktemp -d "${HOME}/.anila-release-src.XXXXXX")"
  git -C "$repo" archive HEAD | tar -C "$clean" -xf -
  set +e
  (
    set -euo pipefail
    _release_build_images_impl "$repo" "$2" "$3" "$4" "$clean"
  )
  rc=$?
  set -e
  rm -rf "$clean"
  return "$rc"
}

main() {
  local out_dir="${1:-$REPO_ROOT/dist/releases}"
  local day ver commit stage lines_file archive_tar
  release_assert_clean "$REPO_ROOT" || exit 1
  day="$(TZ=Asia/Taipei date +%Y.%m.%d)"
  mkdir -p "$out_dir"
  ver="$(release_next_version "$out_dir" "$day")"
  commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  stage="$out_dir/anila-$ver"
  rm -rf "$stage"
  mkdir -p "$stage/images" "$stage/compose"
  info "打包版本 ${ver}（${commit}）"
  git -C "$REPO_ROOT" archive --format=tar HEAD | gzip -c > "$stage/source.tar.gz"
  cp "$REPO_ROOT/compose.yaml" "$stage/compose/compose.yaml"
  cp "$REPO_ROOT/infra/compose/platform.yml" "$stage/compose/platform.yml"
  cp "$SCRIPT_DIR/anila-update.sh" "$stage/anila-update.sh"
  cp "$SCRIPT_DIR/release-lib.sh" "$stage/release-lib.sh"
  cp "$SCRIPT_DIR/images.tsv" "$stage/images.tsv"
  chmod +x "$stage/anila-update.sh"
  lines_file="$(mktemp)"
  release_build_images "$REPO_ROOT" "$stage" "$ver" "$lines_file"
  release_assert_no_secrets "$stage" || die "出貨包含有不該帶的檔案"
  release_seal_manifest "$stage" "$ver" "$commit" "$lines_file"
  rm -f "$lines_file"
  release_verify_bundle "$stage"
  archive_tar="$out_dir/anila-$ver.tar.gz"
  tar -C "$out_dir" -czf "$archive_tar" "anila-$ver"
  sha256sum "$archive_tar"
  ok "已產出 ${archive_tar}"
  printf '內網主機解開後執行：bash anila-%s/anila-update.sh anila-%s\n' "$ver" "$ver"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
