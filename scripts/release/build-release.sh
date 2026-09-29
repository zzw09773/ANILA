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

_release_build_images_impl() {
  local repo="$1" stage="$2" ver="$3" lines="$4" src="$5"
  local override archive svc image start id one have seen scan plain
  local -a args=()
  override="$(mktemp)"
  : > "$lines"
  cat > "$override" <<EOF
services:
  csp:
    build:
      args:
        ANILA_RELEASE_VERSION: "${ver}"
EOF
  info "建置平台映像（含 codeserver、n8n、asr-gateway；內網預設不起 codeserver 與 n8n）"
  # 建置上下文是 HEAD 的乾淨檢出，不是工作目錄。被忽略的檔進不了映像。
  docker compose -f "$src/compose.yaml" -f "$override" build \
    csp-db pgbouncer csp-credential-dirs csp ingestion-worker router nginx \
    pptx-renderer anila-studio anilalm anila-ui codeserver n8n asr-gateway
  rm -f "$override"
  info "取得 redis 映像，一併放進出貨包"
  # redis:7-alpine manifest list，2026-09-29。pull 與 images.tsv 釘這筆 digest。
  # 封存只存 anila-bundle 標籤。load 之後 RepoDigest 不一定還在，主機改用映像 ID 核對。
  docker pull redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499
  declare -A save_args=()
  while read -r svc image archive start; do
    plain="${image%@sha256:*}"
    docker image inspect "$plain" >/dev/null 2>&1 \
      || die "找不到映像 ${plain}。開發機需先建置成功。"
    docker tag "$plain" "anila-bundle/${svc}:${ver}"
    id="$(docker image inspect --format '{{.Id}}' "$plain")"
    printf 'image %s %s %s images/%s.tar.gz\n' "$svc" "$image" "$id" "$archive" >> "$lines"
    save_args["$archive"]+="anila-bundle/${svc}:${ver}"$'\n'
  done < <(release_catalog)
  scan="$src/infra/deployment/scripts/scan-image-artifacts.sh"
  if [[ ! -f "$scan" ]]; then
    scan="$repo/infra/deployment/scripts/scan-image-artifacts.sh"
  fi
  [[ -f "$scan" ]] || die "找不到映像掃描腳本"
  # 每一張映像都掃，含上游 redis。私鑰標頭不因「不是我們建的」而略過。
  local -A scanned=()
  while read -r svc image archive start; do
    [[ -n "${scanned[$image]:-}" ]] && continue
    scanned["$image"]=1
    info "掃描映像 ${image}（私鑰或執行期雜物會中止打包）"
    plain="${image%@sha256:*}"
    bash "$scan" "$plain"
  done < <(release_catalog)
  declare -A scanned_archives=()
  for archive in "${!save_args[@]}"; do
    args=()
    while IFS= read -r one; do
      [[ -n "$one" ]] || continue
      seen=0
      for have in "${args[@]+"${args[@]}"}"; do
        [[ "$have" == "$one" ]] && seen=1 && break
      done
      if (( seen == 0 )); then
        args+=("$one")
      fi
    done < <(printf '%s\n' "${save_args[$archive]}")
    docker save "${args[@]}" | gzip -c > "$stage/images/${archive}.tar.gz"
    # 掃的是 save 出來的封存，一層一層看。後層刪掉的私鑰仍算違規。
    if [[ -z "${scanned_archives[$archive]:-}" ]]; then
      scanned_archives["$archive"]=1
      info "逐層掃描封存 images/${archive}.tar.gz"
      bash "$scan" "$stage/images/${archive}.tar.gz"
    fi
  done
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
