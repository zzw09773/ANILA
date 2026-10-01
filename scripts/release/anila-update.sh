#!/usr/bin/env bash
# anila-update.sh — 內網主機的唯一更新指令。
# 這台沒有網路：只載入出貨包裡的映像，然後 docker compose up --no-build --pull never。
# 不建置、不拉取。
#
#   bash anila-update.sh <出貨包目錄或 tar.gz>
#   bash anila-update.sh rollback
#   bash anila-update.sh adopt <versions/版本目錄>
#
# 沒有安裝記錄時，第一次安裝的預設位置是 /opt/anila。第一次問 ANILA_HOST；
# HTTPS 埠被佔用才再問。密鑰自動產生，只寫進檔案，不印在畫面上。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 壓縮檔裡 manifest.txt 與 manifest.sha256 必須是同一目錄的一對。
# 取最淺的那一對（出貨包根目錄）。同一層有兩對就拒絕，不用清單裡的第一筆。
_pick_bundle_manifest_pair() {
  local list="$1" member dir rest depth best="" best_depth=999 ties=0
  declare -A has_txt=() has_sha=()
  while IFS= read -r member || [[ -n "$member" ]]; do
    member="${member#./}"
    case "$member" in
      */manifest.txt) has_txt["${member%/manifest.txt}"]=1 ;;
      manifest.txt) has_txt["."]=1 ;;
      */manifest.sha256) has_sha["${member%/manifest.sha256}"]=1 ;;
      manifest.sha256) has_sha["."]=1 ;;
    esac
  done < "$list"
  for dir in "${!has_txt[@]}"; do
    [[ -n "${has_sha[$dir]:-}" ]] || continue
    if [[ "$dir" == "." ]]; then
      depth=0
    else
      depth=1
      rest="$dir"
      while [[ "$rest" == */* ]]; do
        depth=$((depth + 1))
        rest="${rest#*/}"
      done
    fi
    if (( depth < best_depth )); then
      best="$dir"
      best_depth=$depth
      ties=0
    elif (( depth == best_depth )); then
      ties=1
    fi
  done
  if [[ -z "$best" ]]; then
    return 1
  fi
  if (( ties == 1 )); then
    printf '出貨包裡有多份清單，拒絕更新。\n' >&2
    return 2
  fi
  if [[ "$best" == "." ]]; then
    printf '%s\n' manifest.txt
    printf '%s\n' manifest.sha256
  else
    printf '%s\n' "$best/manifest.txt"
    printf '%s\n' "$best/manifest.sha256"
  fi
}

# 核對清單本身的 SHA256。必須在 source 任何程式之前，出貨包裡的腳本還沒被讀進來。
# 被測試 source 時不跑：那時沒有出貨包引數。
_entry_manifest_sha256() {
  local spec="${1:-}" tmp txt sha dir
  case "$spec" in
    ""|rollback|adopt|help|-h|--help) return 0 ;;
  esac
  if [[ -d "$spec" ]]; then
    if [[ ! -f "$spec/manifest.sha256" || ! -f "$spec/manifest.txt" ]]; then
      printf '出貨包缺少清單，拒絕更新。\n' >&2
      return 1
    fi
    if ! ( cd "$spec" && sha256sum -c manifest.sha256 ); then
      printf '清單的 SHA256 不符，拒絕更新。\n' >&2
      return 1
    fi
    return 0
  fi
  if [[ ! -f "$spec" ]]; then
    printf '找不到出貨包：%s\n' "$spec" >&2
    return 1
  fi
  local pair_out rc
  # $HOME 在排程或 sudo 下可能沒設。暫存放 ${TMPDIR:-/tmp}，mktemp -d 是 0700。
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/anila-bundle-check.XXXXXX")"
  chmod 700 "$tmp"
  if ! tar -tzf "$spec" > "$tmp/list" 2>/dev/null; then
    rm -rf "$tmp"
    printf '無法讀取出貨包，拒絕更新。\n' >&2
    return 1
  fi
  set +e
  pair_out="$(_pick_bundle_manifest_pair "$tmp/list" 2>"$tmp/pair.err")"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    cat "$tmp/pair.err" >&2
    rm -rf "$tmp"
    if [[ "$rc" -ne 2 ]]; then
      printf '出貨包缺少清單，拒絕更新。\n' >&2
    fi
    return 1
  fi
  txt="$(printf '%s\n' "$pair_out" | awk 'NR==1')"
  sha="$(printf '%s\n' "$pair_out" | awk 'NR==2')"
  if [[ -z "$txt" || -z "$sha" || "$(dirname -- "$txt")" != "$(dirname -- "$sha")" ]]; then
    rm -rf "$tmp"
    printf '出貨包缺少清單，拒絕更新。\n' >&2
    return 1
  fi
  if ! tar -xzf "$spec" -C "$tmp" "$txt" "$sha"; then
    rm -rf "$tmp"
    printf '無法讀取出貨包清單，拒絕更新。\n' >&2
    return 1
  fi
  dir="$tmp/$(dirname -- "$sha")"
  if ! ( cd "$dir" && sha256sum -c manifest.sha256 ); then
    rm -rf "$tmp"
    printf '清單的 SHA256 不符，拒絕更新。\n' >&2
    return 1
  fi
  rm -rf "$tmp"
}

# 清單所在的那一層（出貨包根目錄）。壓縮檔先解開到暫存時，根目錄在上一層。
_bundle_manifest_dir() {
  local root="$1" list pair_out rc txt
  list="$(mktemp "${TMPDIR:-/tmp}/anila-bundle-list.XXXXXX")"
  (cd "$root" && find . -type f -print | sed 's|^\./||') > "$list"
  set +e
  pair_out="$(_pick_bundle_manifest_pair "$list" 2>/dev/null)"
  rc=$?
  set -e
  rm -f "$list"
  [[ "$rc" -eq 0 ]] || return 1
  txt="$(printf '%s\n' "$pair_out" | awk 'NR==1')"
  [[ -n "$txt" ]] || return 1
  if [[ "$txt" == manifest.txt ]]; then
    printf '%s\n' "$root"
  else
    printf '%s\n' "$root/$(dirname -- "$txt")"
  fi
}

# 更新程式必須在清單的 file 行裡，而且雜湊相符。不在或被改過就拒絕。
_bundle_runner_ok() {
  local dir="$1" manifest runner listed got
  manifest="$dir/manifest.txt"
  runner="$dir/anila-update.sh"
  [[ -f "$manifest" ]] || return 1
  listed="$(awk '$1=="file" && $3=="anila-update.sh" { print $2; exit }' "$manifest")"
  if [[ -z "$listed" ]]; then
    printf '出貨包的更新程式不在清單裡，拒絕更新。\n' >&2
    return 1
  fi
  if [[ ! -f "$runner" ]]; then
    printf '出貨包缺少更新程式 anila-update.sh，拒絕更新。\n' >&2
    return 1
  fi
  got="$(sha256sum -- "$runner" | awk '{print $1}')"
  if [[ "$got" != "$listed" ]]; then
    printf '出貨包的更新程式與清單不符，拒絕更新。\n' >&2
    return 1
  fi
  return 0
}

# 這次執行自己建立的解壓目錄。清理只認這個變數，不認呼叫者傳進來的路徑。
_anila_runner_extract_owned=""

# 目錄出貨包：正在跑的就是這包的 anila-update.sh（比的是 realpath，不是環境變數）。
_dir_runner_is_self() {
  local spec="$1" dir runner self
  [[ -d "$spec" ]] || return 1
  dir="$(_bundle_manifest_dir "$spec" 2>/dev/null)" || return 1
  runner="$dir/anila-update.sh"
  [[ -f "$runner" ]] || return 1
  self="$(realpath -- "$0")"
  [[ "$self" == "$(realpath -- "$runner")" ]]
}

# 壓縮檔交接時，父行程把 nonce、自己的 PID、tar SHA256 寫進 stage/owner。
# 子行程只有在自己就是這個 stage 裡的更新程式時才不再交一次。
# 環境變數對得上還不夠：realpath 必須落在這個 stage 裡面。
_claim_is_this_runner() {
  local spec="$1" stage nonce owner_nonce owner_pid owner_sha actual
  local dir runner self stage_real
  [[ -f "$spec" ]] || return 1
  stage="${ANILA_RUNNER_EXTRACT:-}"
  nonce="${ANILA_RUNNER_NONCE:-}"
  [[ -n "$stage" && -n "$nonce" && -d "$stage" && ! -L "$stage" ]] || return 1
  [[ -f "$stage/owner" && ! -L "$stage/owner" ]] || return 1
  [[ -d "$stage/tree" && ! -L "$stage/tree" ]] || return 1
  owner_nonce="$(sed -n '1p' "$stage/owner" || true)"
  owner_pid="$(sed -n '2p' "$stage/owner" || true)"
  owner_sha="$(sed -n '3p' "$stage/owner" || true)"
  [[ "$owner_nonce" == "$nonce" ]] || return 1
  [[ "$owner_pid" == "$PPID" ]] || return 1
  actual="$(sha256sum -- "$spec" | awk '{print $1}')"
  [[ -n "$owner_sha" && "$owner_sha" == "$actual" ]] || return 1
  dir="$(_bundle_manifest_dir "$stage/tree" 2>/dev/null)" || return 1
  runner="$dir/anila-update.sh"
  [[ -f "$runner" && ! -L "$runner" ]] || return 1
  self="$(realpath -- "$0")"
  stage_real="$(realpath -- "$stage")"
  [[ "$self" == "$stage_real"/* ]] || return 1
  [[ "$self" == "$(realpath -- "$runner")" ]]
}

_source_release_lib() {
  if ! declare -F release_verify_bundle >/dev/null 2>&1; then
    # shellcheck source=release-lib.sh
    source "$HERE/release-lib.sh"
  fi
}

# 清單已經核對過。同一版與較舊的出貨包在這裡拒絕，還沒取鎖、也還沒停服務。
# 交接前的舊程式，以及新出貨包自己的更新程式，都呼叫這支。
_version_order_checked=0
_guard_bundle_version() {
  local dir="$1" installed new
  _source_release_lib
  installed="$(state_get current || true)"
  if [[ -z "$installed" || "$installed" == none ]]; then
    _version_order_checked=1
    return 0
  fi
  new="$(manifest_value version "$dir/manifest.txt")"
  if [[ -z "$new" ]]; then
    printf '✗ 清單沒有版本\n' >&2
    return 1
  fi
  _ops_to="$new"
  if ! release_version_order_ok "$new" "$installed"; then
    return 1
  fi
  _version_order_checked=1
  return 0
}

# 目錄直接核對。壓縮檔若交接時已經解開，就用那份；否則先解到暫存，不放進安裝根目錄。
_guard_update_spec() {
  local spec="$1" dir="" stage=""
  _source_release_lib
  if [[ -d "$spec" ]]; then
    if [[ ! -f "$spec/manifest.txt" ]]; then
      printf '✗ 這個目錄不是出貨包（沒有 manifest.txt）\n' >&2
      return 1
    fi
    dir="$spec"
  elif [[ -n "${ANILA_RUNNER_EXTRACT:-}" && -d "${ANILA_RUNNER_EXTRACT}/tree" && ! -L "${ANILA_RUNNER_EXTRACT}" ]]; then
    dir="$(_bundle_manifest_dir "${ANILA_RUNNER_EXTRACT}/tree" 2>/dev/null || true)"
  elif [[ -f "$spec" ]]; then
    stage="$(mktemp -d "${TMPDIR:-/tmp}/anila-order.XXXXXX")"
    chmod 700 "$stage"
    if ! tar -xzf "$spec" -C "$stage"; then
      rm -rf -- "$stage"
      printf '✗ 無法讀取出貨包，拒絕更新。\n' >&2
      return 1
    fi
    dir="$(_bundle_manifest_dir "$stage" 2>/dev/null || true)"
  else
    printf '✗ 找不到出貨包：%s\n' "$spec" >&2
    return 1
  fi
  if [[ -z "$dir" || ! -f "$dir/manifest.txt" ]]; then
    [[ -n "$stage" ]] && rm -rf -- "$stage"
    printf '✗ 出貨包缺少清單，拒絕更新。\n' >&2
    return 1
  fi
  if ! release_verify_bundle "$dir"; then
    [[ -n "$stage" ]] && rm -rf -- "$stage"
    return 1
  fi
  if ! _guard_bundle_version "$dir"; then
    [[ -n "$stage" ]] && rm -rf -- "$stage"
    return 1
  fi
  [[ -n "$stage" ]] && rm -rf -- "$stage"
  return 0
}

# 已安裝的舊更新程式核對完整個出貨包後，把工作交給新出貨包裡那一支。
# 信任根是操作者事先對過的出貨包 tar.gz SHA256，這裡不另做簽章。
# ANILA_RUNNER_HANDOFF 不能單獨跳過；正在跑的腳本必須就是這包的更新程式。
_handoff_to_bundle_runner() {
  local spec="${1:-}" dir="" stage="" runner self target ver nonce tar_sha rc
  case "$spec" in
    ""|rollback|adopt|help|-h|--help) return 0 ;;
  esac
  # 正在跑的就是這包的更新程式。核對之後先比版本，再進更新；舊程式交過來時也走這裡。
  if _dir_runner_is_self "$spec"; then
    _source_release_lib
    dir="$(_bundle_manifest_dir "$spec" 2>/dev/null || true)"
    if [[ -z "$dir" || ! -f "$dir/manifest.txt" ]]; then
      printf '出貨包缺少清單，拒絕更新。\n' >&2
      return 1
    fi
    if ! release_verify_bundle "$dir"; then
      return 1
    fi
    if ! _guard_bundle_version "$dir"; then
      return 1
    fi
    return 0
  fi
  if _claim_is_this_runner "$spec"; then
    _source_release_lib
    dir="$(_bundle_manifest_dir "${ANILA_RUNNER_EXTRACT}/tree" 2>/dev/null || true)"
    if [[ -z "$dir" || ! -f "$dir/manifest.txt" ]]; then
      printf '出貨包缺少清單，拒絕更新。\n' >&2
      return 1
    fi
    if ! release_verify_bundle "$dir"; then
      return 1
    fi
    if ! _guard_bundle_version "$dir"; then
      return 1
    fi
    return 0
  fi
  if [[ -d "$spec" ]]; then
    dir="$spec"
  elif [[ -f "$spec" ]]; then
    # mktemp -d 是 0700。用 sudo 跑時擁有者是 root。不用 $HOME：排程裡它可能沒設。
    stage="$(mktemp -d "${TMPDIR:-/tmp}/anila-runner.XXXXXX")"
    chmod 700 "$stage"
    mkdir -m 700 "$stage/tree"
    if ! tar -xzf "$spec" -C "$stage/tree"; then
      rm -rf -- "$stage"
      printf '無法讀取出貨包，拒絕更新。\n' >&2
      return 1
    fi
    dir="$(_bundle_manifest_dir "$stage/tree")" || {
      rm -rf -- "$stage"
      printf '出貨包缺少清單，拒絕更新。\n' >&2
      return 1
    }
    _anila_runner_extract_owned="$stage"
  else
    return 0
  fi
  if ! _bundle_runner_ok "$dir"; then
    [[ -n "$stage" ]] && rm -rf -- "$stage"
    _anila_runner_extract_owned=""
    return 1
  fi
  # 清單裡的每一個檔都要核對（anila-update.sh、release-lib.sh、images.tsv，以及其他 file 行）。
  _source_release_lib
  if ! release_verify_bundle "$dir"; then
    [[ -n "$stage" ]] && rm -rf -- "$stage"
    _anila_runner_extract_owned=""
    return 1
  fi
  # 比完先後才交給新程式。新程式進來後會再比一次（上面兩個早退）。
  if ! _guard_bundle_version "$dir"; then
    [[ -n "$stage" ]] && rm -rf -- "$stage"
    _anila_runner_extract_owned=""
    return 1
  fi
  runner="$dir/anila-update.sh"
  self="$(realpath -- "$0")"
  target="$(realpath -- "$runner")"
  if [[ "$self" == "$target" ]]; then
    return 0
  fi
  ver="$(awk '$1=="version" {print $2; exit}' "$dir/manifest.txt")"
  printf '交給新出貨包的更新程式 %s\n' "${ver:-未知}"
  export ANILA_RUNNER_HANDOFF=1
  if [[ -n "$stage" ]]; then
    nonce="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    tar_sha="$(sha256sum -- "$spec" | awk '{print $1}')"
    printf '%s\n%s\n%s\n' "$nonce" "$$" "$tar_sha" > "$stage/owner"
    chmod 600 "$stage/owner"
    export ANILA_RUNNER_EXTRACT="$stage"
    export ANILA_RUNNER_NONCE="$nonce"
    # 父行程等子行程結束，再刪自己建立的目錄。不從環境變數取路徑。
    # Ctrl-C、TERM、HUP 時 bash 會先等前景子行程結束才跑 trap，所以中斷也會清掉暫存。
    trap 'rm -rf -- "$stage"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
    set +e
    bash "$target" "$@"
    rc=$?
    set -e
    trap - EXIT INT TERM HUP
    rm -rf -- "$stage"
    _anila_runner_extract_owned=""
    exit "$rc"
  fi
  # 目錄出貨包沒有暫存要清。用 bash 讀腳本，不靠執行位元，noexec 掛載也能跑。
  exec bash "$target" "$@"
}

# 只刪這次執行建立的解壓目錄。呼叫者設的 ANILA_RUNNER_EXTRACT 不算。
# 子行程還在讀腳本時不能刪；tar 交接由父行程等它結束再刪。
_cleanup_runner_extract() {
  local d
  d="${_anila_runner_extract_owned:-}"
  [[ -n "$d" ]] || return 0
  rm -rf -- "$d"
  _anila_runner_extract_owned=""
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  _entry_manifest_sha256 "${1:-}" || exit 1
  _handoff_to_bundle_runner "$@" || exit 1
fi

# shellcheck source=release-lib.sh
source "$HERE/release-lib.sh"

# 載入新映像之後、換成新目錄之前，任何失敗都把 compose 用的標籤指回上一版。
die() {
  if [[ "${ANILA_RETAG_ON_DIE:-0}" == 1 && -n "${ANILA_RETAG_VERSION:-}" && "${_in_die:-0}" != 1 ]]; then
    _in_die=1
    ANILA_RETAG_ON_DIE=0
    retag_compose_from_version "$ANILA_RETAG_VERSION" || true
  fi
  printf '✗ %s\n' "$*" >&2
  exit 1
}

# 命令替換會另開一個 shell。EXIT 只在當初裝上 trap 的那個 shell 做事，
# 避免解壓用的子 shell 先把出貨包刪掉，或把同一筆失敗寫兩次。
_anila_exit_armed=0
_anila_owner_pid=""
_anila_prev_exit_quoted=""
_ops_action=""
_ops_from=""
_ops_to=""
_ops_tree=""
_ops_done=1
_ops_db_ready=0
_update_failure_restart=0
_failure_handled=0
_fail_root=""
_fail_old=""
_fail_new=""
_fail_before=""
_fail_dump=""
_anila_lock_fd=""

_incoming_marker() {
  printf '%s/.incoming-path' "$(install_root)"
}

# 只在這次執行拿著安裝鎖時清。另一個正在進行的更新持有鎖時，不能刪它的解壓目錄。
sweep_stale_incoming() {
  local root d base
  [[ -n "${_anila_lock_fd:-}" ]] || return 0
  root="$(install_root)"
  [[ -d "$root" ]] || return 0
  shopt -s nullglob
  for d in "$root"/incoming.*; do
    [[ -d "$d" && ! -L "$d" ]] || continue
    base="$(basename -- "$d")"
    [[ "$base" == incoming.* ]] || continue
    rm -rf -- "$d"
  done
  shopt -u nullglob
  rm -f "$(_incoming_marker)"
}

cleanup_extracted_bundle() {
  local root lock fd
  if [[ -n "${_anila_lock_fd:-}" ]]; then
    sweep_stale_incoming
    return 0
  fi
  # 解壓在命令替換裡拿過鎖，回到這裡時鎖已經放下。沒有別人持有才清。
  root="$(install_root)"
  [[ -d "$root/state" ]] || return 0
  lock="$root/state/update.lock"
  exec {fd}>"$lock" || return 0
  if flock -n "$fd"; then
    _anila_lock_fd=$fd
    sweep_stale_incoming
    unset _anila_lock_fd
  fi
  exec {fd}>&-
}

# 更新、回復、認領共用一把鎖。第二個執行立刻停，不碰 docker、也不清別人的暫存。
acquire_install_lock() {
  local root lock
  if [[ -n "${_anila_lock_fd:-}" ]]; then
    return 0
  fi
  root="$(install_root)"
  [[ -d "$root" ]] || die "安裝根目錄不存在，不能取得更新鎖。"
  mkdir -p "$root/state"
  lock="$root/state/update.lock"
  exec {_anila_lock_fd}>"$lock" || die "寫不了更新鎖 ${lock}"
  chmod 600 "$lock" || true
  if ! flock -n "$_anila_lock_fd"; then
    exec {_anila_lock_fd}>&-
    unset _anila_lock_fd
    die "另一個更新、回復或認領正在進行。請等它結束再跑。"
  fi
  sweep_stale_incoming
}

_anila_exit() {
  local status=$? prev
  [[ "$BASHPID" == "${_anila_owner_pid:-}" ]] || return "$status"
  if [[ "$status" -ne 0 && "${_update_failure_restart:-0}" == 1 && "${_failure_handled:-0}" == 0 ]]; then
    handle_update_failure || true
  elif [[ "${_ops_done:-1}" == 0 ]]; then
    append_operations_log "${_ops_action:-update}" "${_ops_from:-none}" "${_ops_to:-none}" failure || true
    _ops_done=1
    if [[ "${_ops_db_ready:-0}" == 1 && -n "${_ops_tree:-}" ]]; then
      write_db_audit "$_ops_tree" "${_ops_action:-update}" "${_ops_from:-none}" "${_ops_to:-none}" failure || true
    fi
  fi
  cleanup_extracted_bundle || true
  _cleanup_runner_extract || true
  if [[ -n "${_anila_prev_exit_quoted:-}" ]]; then
    eval "prev=${_anila_prev_exit_quoted}"
    eval "$prev" || true
  fi
  return "$status"
}

_arm_exit() {
  local quoted body
  [[ "${_anila_exit_armed:-0}" == 1 ]] && return 0
  _anila_exit_armed=1
  _anila_owner_pid="$BASHPID"
  quoted="$(trap -p EXIT || true)"
  _anila_prev_exit_quoted=""
  if [[ -n "$quoted" && "$quoted" != *_anila_exit* ]]; then
    body="${quoted#trap -- }"
    body="${body% EXIT}"
    _anila_prev_exit_quoted="$body"
  fi
  trap _anila_exit EXIT
}

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-anila}"
export COMPOSE_PROJECT_NAME

# docker compose --project-directory 的真實路徑必須是安裝根目錄下的
# versions/<版本>，而且版本是記錄裡的目前版、上一版，或這次正在裝的版本。
assert_compose_directory() {
  local tree="$1" phys root base cur prev cand ok=0
  [[ -n "$tree" && -d "$tree" ]] || die "拒絕操作：compose 目錄不存在。"
  phys="$(cd "$tree" && pwd -P)"
  root="$(install_root_phys)"
  if [[ "$(dirname -- "$phys")" != "$root/versions" ]]; then
    die "拒絕操作：compose 目錄必須是 ${root}/versions/<版本>，實際是 ${phys}。"
  fi
  base="$(basename -- "$phys")"
  [[ "$base" =~ ^[A-Za-z0-9._-]+$ ]] || die "拒絕操作：版本目錄名稱不合法。"
  cur="$(state_get current || true)"
  prev="$(state_get previous || true)"
  for cand in "$cur" "$prev" "${_ops_to:-}" "${_fail_old:-}" "${_adopting_version:-}"; do
    if [[ -n "$cand" && "$cand" != "none" && "$cand" == "$base" ]]; then
      ok=1
    fi
  done
  [[ "$ok" == 1 ]] || die "拒絕操作：compose 目錄 ${base} 與安裝記錄不符（目前 ${cur:-無}）。"
}

dc() {
  local tree="$1"
  shift
  local -a extra=()
  assert_destructive_allowed
  assert_compose_directory "$tree"
  if [[ -f "$tree/.anila-images.yml" ]]; then
    extra+=(-f "$tree/.anila-images.yml")
  fi
  docker compose -p "$(compose_project)" --project-directory "$tree" \
    -f "$tree/compose.yaml" "${extra[@]}" "$@"
}

compose_up_no_build() {
  local tree="$1"
  shift
  dc "$tree" up -d --no-build --pull never "$@"
}

current_tree() {
  local root link
  root="$(install_root)"
  link="$root/current"
  if [[ -L "$link" || -d "$link" ]]; then
    readlink -f "$link"
  fi
}

open_bundle() {
  local spec="$1" dest sub root marker oldmask
  if [[ -d "$spec" ]]; then
    [[ -f "$spec/manifest.txt" ]] || die "這個目錄不是出貨包（沒有 manifest.txt）"
    readlink -f "$spec"
    return 0
  fi
  [[ -f "$spec" ]] || die "找不到出貨包：$spec"
  root="$(install_root)"
  mkdir -p "$root"
  # 拿到鎖才清上次留下的解壓目錄。另一個執行持有鎖時，這裡會停、不會刪。
  acquire_install_lock
  dest="$(mktemp -d "${root}/incoming.XXXXXX")"
  marker="$(_incoming_marker)"
  oldmask="$(umask)"
  umask 077
  printf '%s\n' "$dest" > "$marker"
  umask "$oldmask"
  _arm_exit
  tar -xzf "$spec" -C "$dest" || die "無法解開出貨包：$spec"
  sub="$(find "$dest" -mindepth 1 -maxdepth 1 -type d | head -n 1 || true)"
  if [[ -n "$sub" && -f "$sub/manifest.txt" ]]; then
    readlink -f "$sub"
    return 0
  fi
  if [[ -f "$dest/manifest.txt" ]]; then
    readlink -f "$dest"
    return 0
  fi
  die "壓縮檔裡沒有 manifest.txt"
}

db_is_up() {
  local tree="$1"
  [[ -n "$tree" && -f "$tree/compose.yaml" ]] || return 1
  dc "$tree" exec -T csp-db pg_isready -U csp -d csp >/dev/null 2>&1
}

alembic_version() {
  local tree="$1" ver
  ver="$(dc "$tree" exec -T csp-db psql -U csp -d csp -tAc 'SELECT version_num FROM alembic_version' 2>/dev/null || true)"
  printf '%s' "$(printf '%s' "$ver" | tr -d '[:space:]')"
}

banner_count() {
  local tree="$1" n
  n="$(dc "$tree" exec -T csp-db psql -U csp -d csp -tAc 'SELECT count(*) FROM banners WHERE is_active' 2>/dev/null || true)"
  n="$(printf '%s' "$n" | tr -d '[:space:]')"
  [[ "$n" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$n"
}

preupdate_dump() {
  local tree="$1" dest="$2" wrapper lib dir parent oldmask
  dir="$(dirname "$dest")"
  parent="$(dirname "$dir")"
  oldmask="$(umask)"
  umask 077
  mkdir -p "$dir"
  chmod 700 "$dir"
  if [[ "$(basename "$parent")" == "pre-update" ]]; then
    chmod 700 "$parent"
  fi
  lib="$tree/infra/deployment/scripts/backup-lib.sh"
  [[ -f "$lib" ]] || die "這一版原始碼沒有備份程式，無法做更新前備份"
  if [[ -n "${ANILA_PG_DUMP_CMD:-}" ]]; then
    # shellcheck disable=SC1090
    ( . "$lib"; run_pg_dump "$dest" ) || die "更新前的資料庫備份失敗，已停止，還沒有換版本"
  else
    assert_destructive_allowed
    wrapper="$(mktemp)"
    cat > "$wrapper" <<EOF
#!/bin/sh
out=\$1
docker compose -p $(printf '%q' "$(compose_project)") --project-directory $(printf '%q' "$tree") -f $(printf '%q' "$tree/compose.yaml") exec -T csp-db \\
  sh -c 'pg_dump -U "\$POSTGRES_USER" -d "\$POSTGRES_DB" -Fc --no-password' > "\$out"
EOF
    chmod +x "$wrapper"
    # shellcheck disable=SC1090
    ( ANILA_PG_DUMP_CMD="$wrapper"; export ANILA_PG_DUMP_CMD; . "$lib"; run_pg_dump "$dest" ) \
      || die "更新前的資料庫備份失敗，已停止，還沒有換版本"
    rm -f "$wrapper"
  fi
  [[ -s "$dest" ]] || die "更新前的資料庫備份是空的，已停止，還沒有換版本"
  chmod 600 "$dest"
  umask "$oldmask"
}

# 會把使用者資料寫進資料庫或磁碟的服務。csp-db 留著才能備份與還原。
# 舊版 compose 可能沒有 asr-gateway 這類服務，缺了就略過；該停而停不了則中止。
stop_writers() {
  local tree="$1" svc
  assert_destructive_allowed
  local -a must=(
    nginx anila-ui anilalm anila-studio router ingestion-worker
    csp pptx-renderer backup
  )
  local -a optional=(codeserver n8n asr-gateway)
  for svc in "${must[@]}"; do
    dc "$tree" stop "$svc" || die "停不了 ${svc}，不能在它還在寫的時候備份或還原"
  done
  for svc in "${optional[@]}"; do
    dc "$tree" stop "$svc" >/dev/null 2>&1 || true
  done
  dc "$tree" stop pgbouncer || die "停不了 pgbouncer，不能在它還在寫的時候備份或還原"
}

# 資料庫指到的宿主機目錄。硬連結快照不複製位元組；檔案被換掉才佔新空間。
# Studio 成品在具名 volume anila-studio-artifacts，另外用輔助容器打包。
share_snapshot_names() {
  printf '%s\n' uploads attachments static studio-artifacts
}

studio_volume_name() {
  local name
  name="$(docker volume ls \
    --filter "label=com.docker.compose.project=$(compose_project)" \
    --filter "label=com.docker.compose.volume=anila-studio-artifacts" \
    --format '{{.Name}}' | head -n 1 || true)"
  printf '%s' "$name"
}

# 借 csp 映像的 tar 讀寫 studio volume。主機上只有載入時標的專案標籤，
# 清單裡的 anila-csp:latest 只存在開發機（2026-10-01 .35 更新演練：快照是空的）。
studio_helper_image() {
  local svc image archive start
  while read -r svc image archive start; do
    if [[ "$svc" == "csp" ]]; then
      image_project_ref csp running
      return 0
    fi
  done < <(release_catalog)
  return 1
}

snapshot_studio_volume() {
  local dest_parent="$1" vol image
  assert_destructive_allowed
  vol="$(studio_volume_name)"
  [[ -n "$vol" ]] || die "找不到 studio 成品 volume anila-studio-artifacts，不能在沒有這份快照的時候更新"
  image="$(studio_helper_image)" || die "清單沒有 csp 映像，無法讀 studio volume"
  mkdir -p "$dest_parent"
  chmod 700 "$dest_parent"
  docker run --rm --pull never --network none --user 0:0 \
    -v "${vol}:/volume:ro" \
    -v "${dest_parent}:/backup" \
    --entrypoint tar "$image" \
    -C /volume -cf /backup/studio-artifacts.tar .
  [[ -s "$dest_parent/studio-artifacts.tar" ]] || die "studio 成品快照是空的"
  chmod 600 "$dest_parent/studio-artifacts.tar"
}

_studio_run() {
  local snap="$1" script="$2" vol image
  assert_destructive_allowed
  vol="$(studio_volume_name)"
  [[ -n "$vol" ]] || die "找不到 studio 成品 volume，無法還原"
  image="$(studio_helper_image)" || die "清單沒有 csp 映像，無法還原 studio volume"
  docker run --rm --pull never --network none --user 0:0 \
    -v "${vol}:/volume" \
    -v "${snap}:/backup:ro" \
    --entrypoint sh "$image" -c "$script"
}

# 先解到 volume 裡的暫存目錄並核對清單，還不刪線上的檔。
stage_studio_volume() {
  local snap="$1"
  [[ -f "$snap/studio-artifacts.tar" ]] || return 0
  _studio_run "$snap" '
set -eu
rm -rf /volume/.anila-restore-staging
mkdir -p /volume/.anila-restore-staging
tar -C /volume/.anila-restore-staging -xf /backup/studio-artifacts.tar
tar -tf /backup/studio-artifacts.tar >/dev/null
'
}

# 核對過才換上：先把現有內容挪開，再把暫存內容移到 volume 根。
# 快照之後才出現的檔會離開線上目錄。失敗時那些檔還在 .anila-restore-previous。
commit_studio_volume() {
  local snap="$1"
  [[ -f "$snap/studio-artifacts.tar" ]] || return 0
  _studio_run "$snap" '
set -eu
test -d /volume/.anila-restore-staging
rm -rf /volume/.anila-restore-previous
mkdir -p /volume/.anila-restore-previous
find /volume -mindepth 1 -maxdepth 1 ! -name .anila-restore-staging ! -name .anila-restore-previous -exec mv {} /volume/.anila-restore-previous/ \;
find /volume/.anila-restore-staging -mindepth 1 -maxdepth 1 -exec mv {} /volume/ \;
rm -rf /volume/.anila-restore-staging
'
}

revert_studio_volume() {
  local snap="$1"
  [[ -f "$snap/studio-artifacts.tar" ]] || return 0
  _studio_run "$snap" '
set -eu
if [ -d /volume/.anila-restore-previous ]; then
  find /volume -mindepth 1 -maxdepth 1 ! -name .anila-restore-previous -exec rm -rf {} \;
  find /volume/.anila-restore-previous -mindepth 1 -maxdepth 1 -exec mv {} /volume/ \;
  rm -rf /volume/.anila-restore-previous
fi
rm -rf /volume/.anila-restore-staging
' || true
}

cleanup_studio_volume() {
  local snap="$1"
  [[ -f "$snap/studio-artifacts.tar" ]] || return 0
  _studio_run "$snap" '
set -eu
rm -rf /volume/.anila-restore-staging /volume/.anila-restore-previous
' || true
}

# 上傳、附件等目錄屬於服務帳號（10001），權限 700。一般帳號讀不到，硬連結
# 快照與還原都做不成，cp 只印錯誤、快照少一半（2026-10-01 .35 更新演練）。
# 在停任何服務之前檢查，讀不到就停，請操作者改用 sudo。
assert_share_readable() {
  local root name src out
  root="$(install_root)"
  for name in $(share_snapshot_names); do
    src="$root/state/share/$name"
    [[ -d "$src" && ! -L "$src" ]] || continue
    out="$(find "$src" \( ! -readable -o \( -type d ! -executable \) \) -print -quit 2>&1 || true)"
    if [[ -n "$out" ]]; then
      die "讀不到 ${src}（屬於平台的服務帳號），做不了更新前的檔案快照。平台沒有動。請用 sudo 執行這支腳本。"
    fi
  done
}

snapshot_share_files() {
  local dest_parent="$1" root name src oldmask
  root="$(install_root)"
  oldmask="$(umask)"
  umask 077
  mkdir -p "$dest_parent"
  chmod 700 "$dest_parent"
  for name in $(share_snapshot_names); do
    src="$root/state/share/$name"
    [[ -d "$src" && ! -L "$src" ]] || continue
    rm -rf "$dest_parent/$name"
    cp -al "$src" "$dest_parent/$name" || die "檔案快照不完整：${name}。不換版本。"
  done
  umask "$oldmask"
}

# 同一個檔案系統用硬連結。跨裝置（EXDEV）時改成真正的複製。
_copy_tree() {
  local src="$1" dest="$2" err
  err="$(install_root)/state/.copy-err.$$"
  mkdir -p "$(dirname -- "$err")"
  if cp -al "$src" "$dest" 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  if grep -q 'Invalid cross-device link' "$err"; then
    rm -f "$err"
    rm -rf -- "$dest"
    cp -a "$src" "$dest"
    return 0
  fi
  cat "$err" >&2 || true
  rm -f "$err"
  return 1
}

# 快照先複製到暫存目錄並核對，線上目錄先不動。
# 暫存放在安裝根目錄，跟要換上的資料同一顆檔案系統，不放家目錄。
stage_share_files() {
  local dump="$1" snap name src stage parent
  snap="$(dirname -- "$dump")/files"
  [[ -d "$snap" ]] || return 0
  parent="$(install_root)/state/restore-stage"
  mkdir -p "$parent"
  chmod 700 "$parent"
  stage="$(mktemp -d "${parent}/stage.XXXXXX")"
  printf '%s\n' "$stage" > "${dump}.share-stage"
  chmod 600 "${dump}.share-stage"
  for name in $(share_snapshot_names); do
    src="$snap/$name"
    [[ -d "$src" ]] || continue
    if ! _copy_tree "$src" "$stage/$name"; then
      rm -rf "$stage"
      rm -f "${dump}.share-stage"
      return 1
    fi
    if ! diff -rq "$src" "$stage/$name" >/dev/null; then
      rm -rf "$stage"
      rm -f "${dump}.share-stage"
      return 1
    fi
  done
  if ! stage_studio_volume "$snap"; then
    rm -rf "$stage"
    rm -f "${dump}.share-stage"
    return 1
  fi
}

commit_share_files() {
  local dump="$1" snap stage name live root bak
  snap="$(dirname -- "$dump")/files"
  [[ -d "$snap" ]] || return 0
  [[ -f "${dump}.share-stage" ]] || return 1
  stage="$(tr -d '[:space:]' < "${dump}.share-stage")"
  root="$(install_root)"
  for name in $(share_snapshot_names); do
    [[ -d "$stage/$name" ]] || continue
    live="$root/state/share/$name"
    bak="$stage/.bak-$name"
    if [[ -d "$live" && ! -L "$live" ]]; then
      mv "$live" "$bak"
    fi
    mkdir -p "$(dirname -- "$live")"
    if ! _copy_tree "$stage/$name" "$live"; then
      rm -rf "$live"
      if [[ -d "$bak" ]]; then
        mv "$bak" "$live"
      fi
      return 1
    fi
  done
  if ! commit_studio_volume "$snap"; then
    return 1
  fi
}

revert_share_files() {
  local dump="$1" snap stage name live root bak
  snap="$(dirname -- "$dump")/files"
  if [[ -f "${dump}.share-stage" ]]; then
    stage="$(tr -d '[:space:]' < "${dump}.share-stage")"
    root="$(install_root)"
    for name in $(share_snapshot_names); do
      bak="$stage/.bak-$name"
      live="$root/state/share/$name"
      if [[ -d "$bak" ]]; then
        rm -rf "$live"
        mv "$bak" "$live"
      fi
    done
    rm -rf "$stage"
    rm -f "${dump}.share-stage"
  fi
  if [[ -d "$snap" ]]; then
    revert_studio_volume "$snap" || true
  fi
}

cleanup_share_staging() {
  local dump="$1" snap stage
  snap="$(dirname -- "$dump")/files"
  if [[ -d "$snap" ]]; then
    cleanup_studio_volume "$snap" || true
  fi
  if [[ -f "${dump}.share-stage" ]]; then
    stage="$(tr -d '[:space:]' < "${dump}.share-stage")"
    rm -rf "$stage"
    rm -f "${dump}.share-stage"
  fi
}

restore_share_snapshot() {
  local dump="$1"
  stage_share_files "$dump" || return 1
  if ! commit_share_files "$dump"; then
    revert_share_files "$dump" || true
    return 1
  fi
  cleanup_share_staging "$dump"
}

prune_old_preupdate_backups() {
  local root="$1" keep_a="$2" keep_b="$3" dir base parent
  parent="$root/state/share/backups/pre-update"
  [[ -d "$parent" ]] || return 0
  for dir in "$parent"/*; do
    [[ -d "$dir" ]] || continue
    base="$(basename "$dir")"
    [[ "$base" == "$keep_a" || "$base" == "$keep_b" ]] && continue
    rm -rf "$dir"
  done
}

# 先停寫入，再記時間、再 dump、再硬連結快照。時間比 dump 早，之後新增的列才算「會消失」。
# 進入這裡之後若失敗，結束時會把上一版拉起來。
backup_for_update() {
  local tree="$1" dest="$2" stamp ver oldmask
  assert_destructive_allowed
  assert_share_readable
  _update_failure_restart=1
  stop_writers "$tree"
  stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  preupdate_dump "$tree" "$dest"
  oldmask="$(umask)"
  umask 077
  printf '%s\n' "$stamp" > "${dest}.time"
  chmod 600 "${dest}.time"
  ver="$(alembic_version "$tree")"
  [[ -n "$ver" ]] || die "讀不到備份當下的資料庫版本，已停止，還沒有換版本"
  printf '%s\n' "$ver" > "${dest}.alembic"
  chmod 600 "${dest}.alembic"
  snapshot_share_files "$(dirname -- "$dest")/files"
  snapshot_studio_volume "$(dirname -- "$dest")/files"
  umask "$oldmask"
  printf '%s\n' "$stamp"
}

# 映像標籤只落在這個 compose 專案底下。測試與尚未寫入安裝記錄的專案
# 不會改到 anila-csp:latest、redis:7-alpine 或 anila/<服務>。
image_project_ref() {
  printf '%s/%s:%s' "$(compose_project)" "$1" "$2"
}

docker_tag_project() {
  local src="$1" dest="$2"
  [[ "$dest" == "$(compose_project)/"* ]] || die "拒絕操作：映像標籤必須在 $(compose_project)/ 底下，不能標記 ${dest}。"
  docker tag "$src" "$dest"
}

docker_rmi_project() {
  local ref="$1"
  [[ "$ref" == "$(compose_project)/"* ]] || die "拒絕操作：只會刪除 $(compose_project)/ 底下的映像標籤，不會刪 ${ref}。"
  docker rmi "$ref" >/dev/null 2>&1 || true
}

# compose 用的映像名是這次載入後實際標上的專案標籤，不是 manifest-list digest。
write_image_override() {
  local tree="$1" svc image archive start dest
  dest="$tree/.anila-images.yml"
  {
    printf 'services:\n'
    while read -r svc image archive start; do
      printf '  %s:\n    image: %s\n' "$svc" "$(image_project_ref "$svc" running)"
    done < <(release_catalog)
  } > "$dest"
}

tag_running_as_version() {
  local ver="$1" svc image archive start plain running
  assert_destructive_allowed
  [[ -n "$ver" ]] || return 0
  while read -r svc image archive start; do
    running="$(image_project_ref "$svc" running)"
    plain="${image%@sha256:*}"
    if docker image inspect "$running" >/dev/null 2>&1; then
      docker_tag_project "$running" "$(image_project_ref "$svc" "$ver")"
    elif docker image inspect "$plain" >/dev/null 2>&1; then
      docker_tag_project "$plain" "$(image_project_ref "$svc" "$ver")"
    fi
  done < <(release_catalog)
}

retag_compose_from_version() {
  local ver="$1" svc image archive start ref
  assert_destructive_allowed
  [[ -n "$ver" ]] || return 0
  while read -r svc image archive start; do
    ref="$(image_project_ref "$svc" "$ver")"
    if docker image inspect "$ref" >/dev/null 2>&1; then
      docker_tag_project "$ref" "$(image_project_ref "$svc" running)"
    fi
  done < <(release_catalog)
}

load_bundle_images() {
  local bundle="$1" ver="${2:-}" line kind svc image digest archive alt got id
  assert_destructive_allowed
  declare -A loaded=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == image\ * ]] || continue
    alt=""
    read -r kind svc image digest archive alt <<<"$line"
    [[ -f "$bundle/$archive" ]] || die "出貨包缺少映像檔：$archive"
    if [[ -z "${loaded[$archive]:-}" ]]; then
      gzip -dc "$bundle/$archive" | docker load || die "載入映像失敗：$archive"
      loaded["$archive"]=1
    fi
    # 用清單裡的映像 ID 核對。docker load 不會還原 manifest-list 的 RepoDigest。
    # 傳統儲存的 ID 是設定檔雜湊（第四欄），containerd 儲存是 manifest 雜湊
    # （第六欄，2026-09-30 起記）。兩個都出自已核過 SHA256 的同一個封存。
    id=""
    got="$(docker image inspect --format '{{.Id}}' "$digest" 2>/dev/null || true)"
    if [[ "$got" == "$digest" ]]; then
      id="$digest"
    elif [[ -n "$alt" ]]; then
      got="$(docker image inspect --format '{{.Id}}' "$alt" 2>/dev/null || true)"
      [[ "$got" == "$alt" ]] && id="$alt"
    fi
    [[ -n "$id" ]] || die "映像 ${svc} 的內容與清單不符，拒絕繼續"
    if [[ -n "$ver" ]]; then
      docker_tag_project "$id" "$(image_project_ref "$svc" "$ver")"
    fi
    docker_tag_project "$id" "$(image_project_ref "$svc" running)"
  done < "$bundle/manifest.txt"
}

# 舊 intranet-deploy 把 .env 寫在版本目錄（compose.yaml 旁邊）。
# 收進 state/.env，再改成連結。已有內容的 state/.env 不覆蓋。
import_legacy_env() {
  local tree="$1" root="$2" legacy dest oldmask
  dest="$root/state/.env"
  legacy=""
  if [[ -f "$tree/.env" && ! -L "$tree/.env" ]]; then
    legacy="$tree/.env"
  elif [[ -f "$root/.env" && ! -L "$root/.env" ]]; then
    legacy="$root/.env"
  fi
  [[ -n "$legacy" ]] || return 0
  mkdir -p "$root/state"
  if [[ -s "$dest" ]]; then
    return 0
  fi
  oldmask="$(umask)"
  umask 077
  cp "$legacy" "$dest"
  chmod 600 "$dest"
  umask "$oldmask"
}

link_persistent() {
  local tree="$1" root="$2" name
  mkdir -p \
    "$root/state/secrets" \
    "$root/state/share/uploads/ingestion" \
    "$root/state/share/attachments" \
    "$root/state/share/static" \
    "$root/state/share/pki" \
    "$root/state/share/quickstart" \
    "$root/state/share/backups" \
    "$root/state/certs" \
    "$tree/share" \
    "$tree/infra/nginx"
  import_legacy_env "$tree" "$root"
  if [[ ! -e "$root/state/.env" ]]; then
    : > "$root/state/.env"
    chmod 600 "$root/state/.env"
  fi
  ln -sfn "$root/state/.env" "$tree/.env"
  rm -rf "$tree/secrets"
  ln -sfn "$root/state/secrets" "$tree/secrets"
  for name in uploads attachments static pki quickstart backups; do
    rm -rf "$tree/share/$name"
    ln -sfn "$root/state/share/$name" "$tree/share/$name"
  done
  rm -rf "$tree/infra/nginx/certs"
  ln -sfn "$root/state/certs" "$tree/infra/nginx/certs"
}

# 備份檔擁有者與 codeserver 權限。已有非空值就不覆寫。
# UID=、空白、空引號都算沒設，就地換掉那一行。
ensure_host_account() {
  local uid gid docker_gid root
  root="$(install_root)"
  if ! env_has_value UID; then
    if [[ -n "${SUDO_UID:-}" ]]; then
      uid="$SUDO_UID"
    elif [[ -d "$root" ]]; then
      uid="$(stat -c %u "$root")"
    else
      uid="$(stat -c %u .)"
    fi
    set_env UID "$uid"
  fi
  if ! env_has_value GID; then
    if [[ -n "${SUDO_GID:-}" ]]; then
      gid="$SUDO_GID"
    elif [[ -d "$root" ]]; then
      gid="$(stat -c %g "$root")"
    else
      gid="$(stat -c %g .)"
    fi
    set_env GID "$gid"
  fi
  if ! env_has_value DOCKER_GID; then
    docker_gid="$(getent group docker 2>/dev/null | awk -F: '{print $3}' || true)"
    if [[ -n "$docker_gid" ]]; then
      set_env DOCKER_GID "$docker_gid"
    else
      printf '找不到 docker 群組，未寫入 DOCKER_GID。compose 會用預設 999。\n' >&2
    fi
  fi
}

nginx_https_port_of() {
  local file="$1" val=""
  if [[ -f "$file" ]]; then
    val="$(
      cd "$(dirname -- "$file")"
      get_env NGINX_HTTPS_PORT
    )"
  fi
  if [[ -z "$val" ]]; then
    printf '443'
  else
    printf '%s' "$val"
  fi
}

_write_nginx_https_port() {
  local file="$1" port="$2" dir
  dir="$(dirname -- "$file")"
  mkdir -p "$dir"
  if [[ ! -e "$file" && ! -L "$file" ]]; then
    (umask 077; : > "$file")
  fi
  chmod 600 "$file" || true
  (
    cd "$dir"
    set_env NGINX_HTTPS_PORT "$port"
  )
  chmod 600 "$file"
}

# state/.env 裡的埠。沒寫就用預設。
_state_port() {
  local file="$1" key="$2" def="$3" val=""
  if [[ -f "$file" ]]; then
    val="$(
      cd "$(dirname -- "$file")"
      get_env "$key"
    )"
  fi
  if [[ -z "$val" ]]; then
    printf '%s' "$def"
  else
    printf '%s' "$val"
  fi
}

# 這個埠若等於另一個 ANILA 入口（HTTP 或 UI HTTPS），印出那個鍵。
# 值來自 state/.env，沒寫則 NGINX_HTTP_PORT=80、ANILA_UI_HTTPS_PORT=4443。
_anila_port_collision() {
  local file="$1" port="$2" key def other
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  for spec in "NGINX_HTTP_PORT 80" "ANILA_UI_HTTPS_PORT 4443"; do
    read -r key def <<<"$spec"
    other="$(_state_port "$file" "$key" "$def")"
    if port_number_ok "$other" && (( 10#$port == 10#$other )); then
      printf '%s' "$key"
      return 0
    fi
  done
  return 1
}

# 只有第一次安裝會問。埠空著、是 ANILA 的入口、且沒有撞到其他 ANILA 埠，就不問。
# 沒有終端機時拒絕，請操作者把 NGINX_HTTPS_PORT 寫進 state/.env。
# 測試可傳入一個回答；空字串表示採用建議的 8443。正式執行不傳。
ensure_first_install_https_port() {
  local file port choice="" collision="" choice_collision="" blocked=0
  file="$(install_root)/state/.env"
  port="$(nginx_https_port_of "$file")"
  if ! port_number_ok "$port"; then
    blocked=1
  elif collision="$(_anila_port_collision "$file" "$port")"; then
    blocked=1
  elif port_is_listening "$port" && ! anila_nginx_owns_port "$port"; then
    blocked=1
    collision=""
  fi
  if (( blocked == 0 )); then
    return 0
  fi
  if [[ $# -ge 1 ]]; then
    choice="$1"
  elif [[ -t 0 ]]; then
    while true; do
      if [[ -n "$collision" ]]; then
        printf '埠 %s 與 %s 相同，不能當作 HTTPS 埠。要改用哪個 HTTPS 埠？[8443] ' "$port" "$collision" >&2
      else
        printf '埠 %s 已被佔用，不是 ANILA 的入口。要改用哪個 HTTPS 埠？[8443] ' "$port" >&2
      fi
      read -r choice || true
      [[ -z "$choice" ]] && choice=8443
      if ! port_number_ok "$choice"; then
        printf '請輸入 1 到 65535 的數字。\n' >&2
        continue
      fi
      if port_is_listening "$choice" && ! anila_nginx_owns_port "$choice"; then
        printf '埠 %s 也被佔用。\n' "$choice" >&2
        continue
      fi
      if choice_collision="$(_anila_port_collision "$file" "$choice")"; then
        printf '埠 %s 與 %s 相同，不能當作 HTTPS 埠。\n' "$choice" "$choice_collision" >&2
        continue
      fi
      break
    done
  elif [[ -n "$collision" ]]; then
    die "HTTPS 埠 ${port} 與 ${collision} 相同，而且沒有終端機可以詢問。請在 ${file} 寫入 NGINX_HTTPS_PORT=<另一個埠>（例如 8443），chmod 600，然後再執行一次。"
  else
    die "HTTPS 埠 ${port} 已被佔用，而且沒有終端機可以詢問。請在 ${file} 寫入 NGINX_HTTPS_PORT=<另一個埠>（例如 8443），chmod 600，然後再執行一次。"
  fi
  [[ -z "$choice" ]] && choice=8443
  if ! port_number_ok "$choice"; then
    die "HTTPS 埠必須是 1 到 65535 的數字。"
  fi
  if port_is_listening "$choice" && ! anila_nginx_owns_port "$choice"; then
    die "埠 ${choice} 也被佔用。請換一個沒有人用的埠。"
  fi
  if choice_collision="$(_anila_port_collision "$file" "$choice")"; then
    die "埠 ${choice} 與 ${choice_collision} 相同，不能當作 HTTPS 埠。請換一個沒有被 ANILA 其他入口使用的埠。"
  fi
  _write_nginx_https_port "$file" "$choice"
  ok "HTTPS 埠改為 ${choice}"
}

ensure_platform_env() {
  local tree="$1"
  (
    cd "$tree"
    [[ -f .env ]] || die "找不到 .env"
    if [[ -z "$(get_env ANILA_HOST)" ]]; then
      local host
      if [[ ! -t 0 && -z "${ANILA_HOST_ANSWER:-}" ]]; then
        die "還沒有站台名稱。請在終端機執行，以便輸入 ANILA_HOST"
      fi
      if [[ -n "${ANILA_HOST_ANSWER:-}" ]]; then
        host="$ANILA_HOST_ANSWER"
      else
        printf '站台名稱 ANILA_HOST（院內這台的名稱或 IP）：' >&2
        read -r host
      fi
      [[ -n "$host" ]] || die "ANILA_HOST 不能空白"
      ensure_env ANILA_HOST "$host"
    fi
    local newly=() key gen xtrace=0
    # bash -x 會把 set_env 的參數印出來。產生密鑰這段先關掉。
    if [[ $- == *x* ]]; then
      xtrace=1
      set +x
    fi
    while IFS= read -r key; do
      [[ -z "$(get_env "$key")" ]] || continue
      gen="$(openssl rand -hex 32)"
      set_env "$key" "$gen"
      newly+=("$key")
    done <<'EOF'
SECRET_KEY
ADMIN_PASSWORD
CSP_DB_PASSWORD
CSP_APP_DB_PASSWORD
CODESERVER_PASSWORD
EOF
    if (( xtrace )); then
      set -x
    fi
    ensure_env COMPOSE_PROJECT_NAME anila
    ensure_env CODESERVER_WORKSPACE ../..
    ensure_env ANILA_AUTH_MODE card-only
    preserve_flag ANILA_ALLOW_DEV_SECRET "正式環境不允許這個開發旗標，部署前檢查會拒絕"
    ensure_env ANILA_ALLOW_HTTP_ENDPOINT 1
    ensure_env ANILA_ALLOW_PRIVATE_ENDPOINT 1
    ensure_env ANILA_ALLOW_HTTP_AGENT_ENDPOINT 1
    ensure_env ANILA_ALLOW_GRPC_ENDPOINT 1
    preserve_flag CARD_DEV_TRUST_TEST_CA "正式環境不允許信任測試憑證，部署前檢查會拒絕"
    # 逗號經過拆開是空集合：沒有人會因為假員工編號變成擁有者。
    # 擁有者用這次產生的管理員密碼登入。要讓特定員工編號第一次刷卡就是擁有者，
    # 之後改這個既有鍵，再重建 csp。
    ensure_env CARD_INITIAL_OWNERS ","
    if [[ -s share/pki/model-ca.pem ]]; then
      ensure_env ANILA_MODEL_CA_FILE /etc/anila/pki/model-ca.pem
    fi
    ensure_host_account
    if [[ -e .env || -L .env ]]; then
      chmod 600 "$(readlink -f -- .env)"
    fi
    if (( ${#newly[@]} > 0 )); then
      local out xtrace_write=0
      if [[ $- == *x* ]]; then
        xtrace_write=1
        set +x
      fi
      out="$(install_root)/state/generated-secrets.txt"
      umask 077
      {
        printf '這些密鑰只寫在這個檔案，畫面不會印出內容。請抄進密碼管理器。\n'
        for key in "${newly[@]}"; do
          printf '%s=%s\n' "$key" "$(get_env "$key")"
        done
      } > "$out"
      chmod 600 "$out"
      if (( xtrace_write )); then
        set -x
      fi
      if [[ "$(id -u)" -eq 0 ]]; then
        chown root:root "$out" || true
      fi
      # 值只在檔案裡。標準輸出與標準錯誤只給路徑與讀取指令。
      printf '已產生密鑰，寫在 %s（權限 600）。\n' "$out"
      printf '請用下面的指令讀取，抄進密碼管理器：\n'
      printf '  sudo cat %s\n' "$out"
      printf '卡片首次擁有者尚未指定。請用管理員密碼登入；需要的話再改既有的 CARD_INITIAL_OWNERS。\n'
    fi
  )
}

ensure_tls() {
  local root="$1" tree="$2" host crt key
  crt="$root/state/certs/server.crt"
  key="$root/state/certs/server.key"
  if [[ -s "$crt" && -s "$key" ]]; then
    ok "沿用現有的 TLS 憑證"
    return 0
  fi
  host="$(cd "$tree" && get_env ANILA_HOST)"
  [[ -n "$host" ]] || die "沒有 ANILA_HOST，無法簽臨時憑證"
  info "沒有現成的 TLS 憑證，先簽一張給 ${host} 的自簽憑證。院內正式憑證請之後換成同一路徑。"
  umask 077
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$key" -out "$crt" -days 825 -subj "/CN=${host}" >/dev/null 2>&1
  chmod 600 "$key"
  chmod 644 "$crt"
}

ensure_model_ca() {
  local tree="$1" dest src
  dest="$tree/share/pki/model-ca.pem"
  src="$tree/services/csp/app/services/cspki_ca_bundle.pem"
  if [[ -s "$dest" ]]; then
    return 0
  fi
  if [[ -s "$src" ]]; then
    cp "$src" "$dest"
    ok "已放入公開的院內 CA，供模型連線使用"
  fi
}

ensure_jwt() {
  local tree="$1" image
  # 主機上只有載入時標的專案標籤。compose 裡的 anila-csp:latest 只存在開發機
  # (2026-09-30 .35 演練：第一次安裝停在這一步)。
  image="$(image_project_ref csp running)"
  if [[ -s "$tree/secrets/jwt-private.pem" && -s "$tree/secrets/jwt-public.pem" ]]; then
    ok "沿用現有的 JWT 簽章金鑰"
    return 0
  fi
  docker image inspect "$image" >/dev/null 2>&1 || die "找不到映像 ${image}，無法產生 JWT 簽章金鑰"
  docker run --rm --pull never --user 0:0 --network none \
    -v "$tree/secrets:/out" \
    --entrypoint python "$image" \
    /app/scripts/generate-jwt-keypair.py --output-dir /out
  ok "已產生 JWT 簽章金鑰"
}

fix_ownership() {
  local tree="$1"
  bash "$tree/infra/deployment/scripts/fix-runtime-ownership.sh" \
    "$(image_project_ref csp running)"
}

service_is_ready() {
  local svc="$1" status="$2" low
  low="$(printf '%s' "$status" | tr '[:upper:]' '[:lower:]')"
  [[ "$low" == *unhealthy* ]] && return 1
  # 不要寫成「[[ … ]]; return」。在 EXIT trap 裡，不帶數字的 return 回傳的是
  # 進 trap 之前的結束碼（die 的 1），失敗後回復的健康檢查會永遠不過、入口不開
  # （2026-10-01 .35 更新演練實撞）。
  if [[ "$svc" == "csp-credential-dirs" ]]; then
    [[ "$low" == *"exited (0)"* ]] && return 0
    return 1
  fi
  if [[ "$low" == *"(healthy)"* ]]; then
    return 0
  fi
  if [[ "$low" == *"health:"* ]]; then
    return 1
  fi
  [[ "$low" == up* || "$low" == *running* ]]
}

health_ok() {
  local tree="$1" line svc status
  declare -A want=()
  while IFS= read -r svc; do
    [[ -n "$svc" && "$svc" != "nginx" ]] && want["$svc"]=1
  done < <(services_to_start)
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    svc="${line%% *}"
    status="${line#* }"
    [[ -n "${want[$svc]:-}" ]] || continue
    service_is_ready "$svc" "$status" || return 1
    unset "want[$svc]"
  done < <(dc "$tree" ps -a --format '{{.Service}} {{.Status}}' 2>/dev/null || true)
  (( ${#want[@]} == 0 )) || return 1
  dc "$tree" exec -T csp curl -sf http://127.0.0.1:8000/health >/dev/null
}

wait_healthy() {
  local tree="$1" timeout start now
  timeout="${ANILA_HEALTH_TIMEOUT:-300}"
  start="$(date +%s)"
  while true; do
    if health_ok "$tree"; then
      ok "服務健康，CSP /health 正常"
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= timeout )); then
      printf '健康檢查逾時（%s 秒）。CSP /health 還沒就緒。\n' "$timeout" >&2
      return 1
    fi
    sleep "${ANILA_HEALTH_POLL:-5}"
  done
}

switch_current() {
  local root="$1" ver="$2"
  ln -sfn "versions/${ver}" "$root/current"
  info "目前指標改為 ${ver}"
}

restore_dump() {
  local tree="$1" dump="$2" expect got err rc
  assert_destructive_allowed
  [[ -f "$dump" ]] || die "找不到更新前的資料庫備份：$dump"
  [[ -f "${dump}.alembic" ]] || die "備份沒有資料庫版本，拒絕還原"
  expect="$(tr -d '[:space:]' < "${dump}.alembic")"
  [[ "$expect" =~ ^[0-9A-Za-z_]+$ ]] || die "備份裡的資料庫版本不正確"
  info "還原更新前的資料庫備份"
  stop_writers "$tree"
  dc "$tree" cp "$dump" csp-db:/tmp/anila-pre-update.dump
  # 先還原進空資料庫。核對版本通過才換掉線上那顆，失敗時原本的庫還在。
  dc "$tree" exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'csp_restore' AND pid <> pg_backend_pid()" \
    -c "DROP DATABASE IF EXISTS csp_restore" \
    -c "CREATE DATABASE csp_restore OWNER csp TEMPLATE template0"
  # template0 仍有 public。pg_dump 會再建一次，--exit-on-error 會停在「已經存在」。
  dc "$tree" exec -T csp-db psql -U csp -d csp_restore -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA IF EXISTS public CASCADE"
  err="$(mktemp)"
  set +e
  dc "$tree" exec -T csp-db \
    pg_restore -U csp -d csp_restore --no-owner --exit-on-error /tmp/anila-pre-update.dump \
    >"$err" 2>&1
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    cat "$err" >&2
    rm -f "$err"
    dc "$tree" exec -T csp-db psql -U csp -d postgres -c "DROP DATABASE IF EXISTS csp_restore" >/dev/null 2>&1 || true
    die "資料庫還原失敗。原本的資料庫還在。請先看上方訊息，不要重跑更新。"
  fi
  rm -f "$err"
  got="$(dc "$tree" exec -T csp-db psql -U csp -d csp_restore -tAc 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')"
  if [[ "$got" != "$expect" ]]; then
    dc "$tree" exec -T csp-db psql -U csp -d postgres -c "DROP DATABASE IF EXISTS csp_restore" >/dev/null 2>&1 || true
    die "還原後的資料庫版本是 ${got:-（空白）}，備份時是 ${expect}。原本的資料庫還在。"
  fi
  # 線上庫改名保留，等健康檢查過了才丟掉。檢查沒過可以改回來。
  dc "$tree" exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('csp', 'csp_restore', 'csp_previous') AND pid <> pg_backend_pid()" \
    -c "DROP DATABASE IF EXISTS csp_previous" \
    -c "ALTER DATABASE csp RENAME TO csp_previous" \
    -c "ALTER DATABASE csp_restore RENAME TO csp"
  ok "已還原更新前的資料庫，原本的庫先改名保留"
}

revert_preserved_database() {
  local tree="$1"
  assert_destructive_allowed
  dc "$tree" exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('csp', 'csp_previous', 'csp_rollback_new') AND pid <> pg_backend_pid()" \
    -c "ALTER DATABASE csp RENAME TO csp_rollback_new" \
    -c "ALTER DATABASE csp_previous RENAME TO csp" \
    -c "DROP DATABASE IF EXISTS csp_rollback_new"
}

drop_preserved_database() {
  local tree="$1"
  assert_destructive_allowed
  dc "$tree" exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS csp_previous"
}

created_since_counts() {
  local tree="$1" since="$2"
  [[ "$since" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
    || die "備份時間的格式不對，拒絕回復"
  dc "$tree" exec -T csp-db psql -U csp -d csp -tAc \
    "SELECT (SELECT count(*) FROM conversations WHERE created_at > timestamptz '${since}'), (SELECT count(*) FROM messages WHERE created_at > timestamptz '${since}'), (SELECT count(*) FROM ingestion_documents WHERE uploaded_at > timestamptz '${since}')"
}

# 同一個目標版本的最近一筆若是失敗，而且操作種類與這次相同，成功紀錄才註記接續。
# 安裝是 from=none（或 action=install）；更新是 action=update 且 from 是版本。
# 最近一筆若是 rollback 或 adopt，不算未完成的安裝或更新。
_unfinished_note() {
  local from="$1" to="$2" file="$3"
  local act="" f="" to_f="" result_f="" last_result="" last_act="" last_from=""
  [[ -f "$file" ]] || return 0
  while IFS=$'\t' read -r _ts _op act f to_f result_f _extra || [[ -n "${act:-}" ]]; do
    [[ -n "${to_f:-}" ]] || continue
    [[ "$to_f" == "$to" ]] || continue
    last_result="${result_f:-}"
    last_act="${act:-}"
    last_from="${f:-}"
  done < "$file"
  [[ "$last_result" == failure ]] || return 0
  case "$last_act" in
    update|install) ;;
    *) return 0 ;;
  esac
  if [[ "$from" == "none" || -z "$from" ]]; then
    if [[ "$last_act" == install || "$last_from" == none || -z "$last_from" ]]; then
      printf '接續上次未完成的安裝'
    fi
    return 0
  fi
  if [[ "$last_act" == update && -n "$last_from" && "$last_from" != none ]]; then
    printf '接續上次未完成的更新'
  fi
  return 0
}

append_operations_log() {
  local action="$1" from="$2" to="$3" result="$4"
  local file op oldmask note=""
  case "$action" in
    update|rollback|adopt) ;;
    *) die "未知的操作紀錄：$action" ;;
  esac
  case "$result" in
    success|failure) ;;
    *) die "未知的操作結果：$result" ;;
  esac
  file="$(install_root)/state/operations.log"
  # 拒絕時安裝根目錄可能還不存在。不要為了寫紀錄而建立 /opt/anila。
  [[ -d "$(install_root)" ]] || return 0
  if [[ "$result" == success && "$action" == update ]]; then
    note="$(_unfinished_note "$from" "$to" "$file")"
  fi
  mkdir -p "$(dirname "$file")"
  oldmask="$(umask)"
  umask 077
  touch "$file"
  chmod 600 "$file"
  op="$(id -un)"
  if [[ -n "$note" ]]; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$op" "$action" "$from" "$to" "$result" "$note" >> "$file"
  else
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$op" "$action" "$from" "$to" "$result" >> "$file"
  fi
  chmod 600 "$file"
  umask "$oldmask"
}

_sql_quote() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf '%s' "$s"
}

_pending_audit_file() {
  printf '%s/state/db-audit-pending' "$(install_root)"
}

# 直接對 csp-db 下 psql。csp 容器沒起來也寫得了。失敗不在這裡標待寫。
_audit_insert() {
  local tree="$1" action="$2" from="$3" to="$4" result="$5" op="$6" sql
  op="${op:0:100}"
  case "$action" in
    update|rollback|adopt) ;;
    *) return 1 ;;
  esac
  case "$result" in
    success|failure) ;;
    *) return 1 ;;
  esac
  sql="$(printf "INSERT INTO audit_logs (actor_user_id, actor_username, action, resource_type, resource_id, status, detail, created_at) VALUES (NULL, '%s', 'platform_%s', 'platform_release', '%s', '%s', '%s → %s', now())" \
    "$(_sql_quote "$op")" "$(_sql_quote "$action")" "$(_sql_quote "$to")" \
    "$(_sql_quote "$result")" "$(_sql_quote "$from")" "$(_sql_quote "$to")")"
  dc "$tree" exec -T csp-db psql -U csp -d csp -v ON_ERROR_STOP=1 -c "$sql"
}

mark_db_audit_pending() {
  local action="$1" from="$2" to="$3" result="$4" op="$5" file oldmask
  [[ -d "$(install_root)" ]] || return 0
  file="$(_pending_audit_file)"
  oldmask="$(umask)"
  umask 077
  mkdir -p "$(dirname "$file")"
  printf '%s\t%s\t%s\t%s\t%s\n' "$action" "$from" "$to" "$result" "${op:0:100}" >> "$file"
  chmod 600 "$file"
  umask "$oldmask"
  state_set db_audit_pending 1
}

write_db_audit() {
  local tree="$1" action="$2" from="$3" to="$4" result="$5" op
  op="$(id -un)"
  op="${op:0:100}"
  if _audit_insert "$tree" "$action" "$from" "$to" "$result" "$op"; then
    return 0
  fi
  mark_db_audit_pending "$action" "$from" "$to" "$result" "$op"
  return 1
}

retry_pending_db_audits() {
  local tree="$1" file action from to result op tmp
  file="$(_pending_audit_file)"
  if [[ ! -s "$file" ]]; then
    if [[ -d "$(install_root)/state" ]]; then
      state_set db_audit_pending 0
    fi
    return 0
  fi
  tmp="$(mktemp)"
  while IFS=$'\t' read -r action from to result op || [[ -n "${action:-}" ]]; do
    [[ -n "${action:-}" ]] || continue
    if _audit_insert "$tree" "$action" "$from" "$to" "$result" "$op"; then
      continue
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$action" "$from" "$to" "$result" "$op" >> "$tmp"
  done < "$file"
  if [[ -s "$tmp" ]]; then
    chmod 600 "$tmp"
    mv -f "$tmp" "$file"
    chmod 600 "$file"
    state_set db_audit_pending 1
    rm -f "$tmp"
    return 1
  fi
  rm -f "$tmp" "$file"
  state_set db_audit_pending 0
  return 0
}

record_audit() {
  local tree="$1" action="$2" from="$3" to="$4" result="$5" op
  op="$(id -un)"
  append_operations_log "$action" "$from" "$to" "$result"
  _ops_done=1
  if write_db_audit "$tree" "$action" "$from" "$to" "$result"; then
    ok "已寫入稽核（${op}：${from} → ${to}，${result}）"
    return 0
  fi
  warn "平台稽核沒寫進資料庫，主機紀錄在 $(install_root)/state/operations.log"
}

prune_old_images() {
  local keep_a="$1" keep_b="$2" svc image archive start ref tag ns
  ns="$(compose_project)"
  while read -r svc image archive start; do
    while IFS= read -r ref; do
      [[ "$ref" == "${ns}/${svc}:"* ]] || continue
      tag="${ref##*:}"
      [[ "$tag" == "$keep_a" || "$tag" == "$keep_b" || "$tag" == "running" || "$tag" == "<none>" ]] && continue
      docker_rmi_project "$ref"
    done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' "${ns}/${svc}" 2>/dev/null || true)
  done < <(release_catalog)
}

prune_old_trees() {
  local root="$1" keep_a="$2" keep_b="$3" dir base
  [[ -d "$root/versions" ]] || return 0
  for dir in "$root/versions"/*; do
    [[ -d "$dir" ]] || continue
    base="$(basename "$dir")"
    [[ "$base" == "$keep_a" || "$base" == "$keep_b" ]] && continue
    rm -rf "$dir"
  done
}

start_platform() {
  local tree="$1"
  local -a svcs=()
  local s
  write_image_override "$tree"
  while IFS= read -r s; do
    [[ -n "$s" && "$s" != "nginx" ]] && svcs+=("$s")
  done < <(services_to_start)
  # 入口先不開。內部健康檢查過了才啟動 nginx。
  # 預設不起 codeserver / n8n。最後演練才另外點名這兩個服務。
  compose_up_no_build "$tree" "${svcs[@]}"
}

entry_ready() {
  local tree="$1" line status code port
  line="$(dc "$tree" ps -a --format '{{.Service}} {{.Status}}' 2>/dev/null | awk '$1=="nginx" { print; exit }' || true)"
  [[ -n "$line" ]] || return 1
  status="${line#* }"
  service_is_ready nginx "$status" || return 1
  # 443 被別的服務佔用時，.env 設 NGINX_HTTPS_PORT，入口檢查跟著走同一個埠。
  port="$(cd "$tree" && get_env NGINX_HTTPS_PORT)"
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || port=443
  code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "https://127.0.0.1:${port}/login" || true)"
  [[ "$code" == "200" ]]
}

open_entry_command() {
  local tree="$1" project
  project="$(compose_project)"
  if [[ -f "$tree/.anila-images.yml" ]]; then
    printf 'docker compose -f %q -f %q -p %q up -d --no-build --pull never nginx' \
      "$tree/compose.yaml" "$tree/.anila-images.yml" "$project"
  else
    printf 'docker compose -f %q -p %q up -d --no-build --pull never nginx' \
      "$tree/compose.yaml" "$project"
  fi
}

# 自動回復結束時入口沒開，要直接說同仁連不上，並給出開入口的指令。
report_closed_entry() {
  local tree="$1" cmd
  [[ -n "$tree" && -f "$tree/compose.yaml" ]] || return 0
  if entry_ready "$tree"; then
    return 0
  fi
  cmd="$(open_entry_command "$tree")"
  printf '入口未開啟，同仁目前連不上。\n' >&2
  printf '%s\n' "$cmd" >&2
  return 0
}

open_entry() {
  local tree="$1" timeout start now
  compose_up_no_build "$tree" nginx || return 1
  timeout="${ANILA_HEALTH_TIMEOUT:-300}"
  start="$(date +%s)"
  while true; do
    if entry_ready "$tree"; then
      ok "入口已開啟，登入頁回應 200"
      return 0
    fi
    now="$(date +%s)"
    if (( now - start >= timeout )); then
      dc "$tree" stop nginx >/dev/null 2>&1 || true
      printf '入口健康檢查逾時（%s 秒）。登入頁還沒就緒，入口已停下。\n' "$timeout" >&2
      return 1
    fi
    sleep "${ANILA_HEALTH_POLL:-5}"
  done
}

bring_up() {
  local tree="$1"
  start_platform "$tree" || return 1
  wait_healthy "$tree" || return 1
  open_entry "$tree" || return 1
}

restart_platform() {
  bring_up "$1" || true
}

unpack_source() {
  local bundle="$1" dest="$2"
  mkdir -p "$dest"
  tar -xzf "$bundle/source.tar.gz" -C "$dest"
  [[ -f "$dest/compose.yaml" ]] || die "原始碼封存裡沒有 compose.yaml"
}

prepare_tree() {
  local tree="$1" root="$2"
  link_persistent "$tree" "$root"
  ensure_model_ca "$tree"
  ensure_platform_env "$tree" || die "寫入站台設定失敗"
  # shellcheck source=../../infra/deployment/scripts/prod-env-guard.sh
  source "$tree/infra/deployment/scripts/prod-env-guard.sh"
  prod_env_refuse "$tree/.env" || die "正式部署條件不符"
  ensure_tls "$root" "$tree"
  ensure_jwt "$tree"
  fix_ownership "$tree"
}

# 停寫入之後任何失敗都走這裡：拉起上一版。
# 目前的 alembic 版本與備份記下的版本不同，才還原資料庫與檔案。
handle_update_failure() {
  local root old new before dump tree after
  [[ "${_failure_handled:-0}" == 1 ]] && return 0
  _failure_handled=1
  root="${_fail_root:-}"
  old="${_fail_old:-}"
  new="${_fail_new:-}"
  before="${_fail_before:-}"
  dump="${_fail_dump:-}"
  if [[ "${_ops_done:-1}" == 0 ]]; then
    append_operations_log update "${old:-none}" "${new:-none}" failure || true
    _ops_done=1
  fi
  if [[ -z "$old" || "$old" == "none" ]]; then
    return 0
  fi
  retag_compose_from_version "$old" || true
  if [[ -n "$root" && -d "$root/versions/$old" ]]; then
    switch_current "$root" "$old" || true
  fi
  tree="$(current_tree || true)"
  if [[ -n "$tree" && -n "$before" && -n "$dump" && -f "$dump" ]]; then
    after="$(alembic_version "$tree" || true)"
    if rollback_needs_db_restore "$before" "$after"; then
      if ( restore_dump "$tree" "$dump" ); then
        restore_share_snapshot "$dump" || warn "檔案快照還原失敗。"
      else
        warn "資料庫還原失敗。線上資料庫沒有被換掉，仍嘗試啟動上一版。"
      fi
    fi
  fi
  if [[ -n "$tree" && -f "$tree/compose.yaml" ]]; then
    restart_platform "$tree"
    _ops_tree="$tree"
  fi
  if [[ "${_ops_db_ready:-0}" == 1 && -n "${_ops_tree:-}" ]]; then
    write_db_audit "$_ops_tree" "${_ops_action:-update}" "${_ops_from:-none}" "${_ops_to:-none}" failure || true
  fi
}

_note_rollback_failure() {
  local tree="$1" new="$2" old="$3" message="$4"
  if [[ -n "$tree" && -f "$tree/compose.yaml" ]]; then
    restart_platform "$tree"
    _ops_tree="$tree"
    _ops_db_ready=1
  fi
  append_operations_log rollback "$new" "$old" failure || true
  if [[ "${_ops_db_ready:-0}" == 1 && -n "${_ops_tree:-}" ]]; then
    write_db_audit "$_ops_tree" rollback "$new" "$old" failure || true
    write_db_audit "$_ops_tree" update "${old:-none}" "$new" failure || true
  else
    mark_db_audit_pending rollback "$new" "$old" failure "$(id -un)" || true
    mark_db_audit_pending update "${old:-none}" "$new" failure "$(id -un)" || true
  fi
  _ops_done=1
  printf '%s\n' "$message" >&2
  report_closed_entry "$tree"
}

auto_rollback() {
  local root="$1" old="$2" new="$3" before="$4" dump="$5"
  local old_tree after op
  assert_destructive_allowed
  _failure_handled=1
  # 拒絕回復也要留紀錄，所以在看版本目錄之前就打開失敗紀錄。
  _ops_action=rollback
  _ops_from="${new:-none}"
  _ops_to="${old:-none}"
  _ops_done=0
  _ops_db_ready=0
  _ops_tree=""
  info "更新沒有完成，開始自動回復到 ${old:-（沒有上一版）}"
  # 先把 compose 用的標籤指回上一版，再看版本目錄在不在。
  retag_compose_from_version "$old" || true
  if [[ -z "$old" || ! -d "$root/versions/$old" ]]; then
    append_operations_log rollback "${new:-none}" "${old:-none}" failure || true
    mark_db_audit_pending rollback "${new:-none}" "${old:-none}" failure "$(id -un)" || true
    _ops_done=1
    printf '初次安裝失敗，沒有上一版可回復。資料庫 volume 還在。請看：docker compose -p %s logs --tail 80 csp\n' \
      "$(compose_project)" >&2
    return 1
  fi
  switch_current "$root" "$old"
  old_tree="$(current_tree)"
  after="$(alembic_version "$old_tree")"
  if rollback_needs_db_restore "$before" "$after"; then
    info "目前的資料庫版本與備份不同，還原更新前的備份"
    if ! stage_share_files "$dump"; then
      _note_rollback_failure "$old_tree" "$new" "$old" \
        "自動回復時檔案快照還原失敗。線上資料庫沒有被換掉，服務已指回 ${old}。"
      return 1
    fi
    if ! commit_share_files "$dump"; then
      revert_share_files "$dump" || true
      _note_rollback_failure "$old_tree" "$new" "$old" \
        "自動回復時檔案快照還原失敗。線上資料庫沒有被換掉，服務已指回 ${old}。"
      return 1
    fi
    if ( restore_dump "$old_tree" "$dump" ); then
      cleanup_share_staging "$dump"
    else
      # 還原停在暫存庫。線上的 csp 沒有被刪掉或改名。檔案換上的部分退回。
      revert_share_files "$dump" || true
      _note_rollback_failure "$old_tree" "$new" "$old" \
        "自動回復時資料庫還原失敗。線上資料庫沒有被換掉，服務已指回 ${old}。"
      return 1
    fi
  else
    ok "目前的資料庫版本與備份相同，不還原資料庫"
  fi
  _ops_tree="$old_tree"
  _ops_db_ready=1
  op="$(id -un)"
  if bring_up "$old_tree"; then
    drop_preserved_database "$old_tree" || true
    append_operations_log rollback "$new" "$old" success
    write_db_audit "$old_tree" rollback "$new" "$old" success || true
    write_db_audit "$old_tree" update "${old:-none}" "$new" failure || true
    _ops_done=1
    ok "已回復到 ${old}"
    ok "已寫入稽核（${op}：${new} → ${old}，success）"
    return 0
  fi
  append_operations_log rollback "$new" "$old" failure
  write_db_audit "$old_tree" rollback "$new" "$old" failure || true
  write_db_audit "$old_tree" update "${old:-none}" "$new" failure || true
  _ops_done=1
  printf '自動回復後健康檢查仍失敗。目前指標指回 %s，請看容器日誌。\n' "$old" >&2
  report_closed_entry "$old_tree"
  return 1
}

install_runner() {
  local bundle="$1" root="$2"
  mkdir -p "$root"
  cp "$bundle/anila-update.sh" "$root/anila-update.sh.new"
  cp "$bundle/release-lib.sh" "$root/release-lib.sh.new"
  cp "$bundle/images.tsv" "$root/images.tsv.new"
  chmod +x "$root/anila-update.sh.new"
  mv -f "$root/anila-update.sh.new" "$root/anila-update.sh"
  mv -f "$root/release-lib.sh.new" "$root/release-lib.sh"
  mv -f "$root/images.tsv.new" "$root/images.tsv"
}

die_first_install_location() {
  local spec="$1" name
  name="$(basename -- "$spec")"
  name="${name%.tar.gz}"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || name="anila-YYYY.MM.DD-N"
  die "拒絕操作：第一次安裝必須解在 /opt/anila。請執行：
tar -xzf ${name}.tar.gz -C /opt/anila
bash /opt/anila/${name}/anila-update.sh /opt/anila/${name}"
}

# 第一次安裝、還沒改任何東西之前：安裝根目錄與（正式專案）/var/lib/anila 要能放得下。
# 檢查順序與 preflight 相同。不是 root 就印出這次要改跑的 sudo 指令。
_refuse_unready_install_dirs() {
  local spec="$1" root anchor="" bad cmd
  root="$(install_root)"
  if [[ "$(compose_project)" == "anila" ]]; then
    anchor="$(dirname -- "$(_install_anchor_file)")"
  fi
  bad="$(collect_unready_install_dirs "$root" "$anchor")"
  [[ -n "$bad" ]] || return 0
  cmd="$(printf 'sudo bash %q %q' "${BASH_SOURCE[0]}" "$spec")"
  die "$(printf '下面的目錄不存在或不可寫：\n%s\n目前不是 root。請改用：\n%s' "$bad" "$cmd")"
}

cmd_update() {
  local spec="$1" bundle root old old_tree new dest before dump now stamp older count
  _ops_action=update
  _ops_from=none
  _ops_to=none
  _ops_done=0
  _ops_db_ready=0
  _ops_tree=""
  _update_failure_restart=0
  _failure_handled=0
  _fail_root=""
  _fail_old=""
  _fail_new=""
  _fail_before=""
  _fail_dump=""
  _arm_exit
  root="$(install_root)"
  old="$(state_get current || true)"
  _ops_from="${old:-none}"
  # 交接流程裡新程式已經比過。直接呼叫更新時，在取鎖與解壓到安裝根目錄之前比。
  if [[ "${_version_order_checked:-0}" != 1 && -n "$old" && "$old" != none ]]; then
    _guard_update_spec "$spec" || exit 1
  fi
  if [[ -z "$old" || "$old" == "none" ]]; then
    refuse_if_anchor_mismatch
    if [[ "$(compose_project)" == "anila" ]] && ! install_root_is_real_anchor; then
      die_first_install_location "$spec"
    fi
    _refuse_unready_install_dirs "$spec"
    if [[ -d "$root" ]]; then
      acquire_install_lock
    fi
    # 沒有 release.state / anchor，但這台已有同名專案或 volume：不是第一次安裝。
    refuse_if_existing_stack
  fi
  assert_destructive_allowed
  if [[ -n "$old" && "$old" != "none" ]]; then
    acquire_install_lock
    old_tree="$(current_tree || true)"
    if [[ -z "$old_tree" || ! -f "$old_tree/compose.yaml" ]]; then
      die "已有上一版 ${old}，但找不到版本目錄。這不是第一次安裝。已停止，還沒有改任何東西。"
    fi
    if ! db_is_up "$old_tree"; then
      die "已有上一版 ${old}，但資料庫沒有回應。這不是第一次安裝，不能略過備份。已停止，還沒有改版本、也沒有載入映像。"
    fi
    _ops_tree="$old_tree"
    _ops_db_ready=1
    retry_pending_db_audits "$old_tree" || true
  fi
  command -v docker >/dev/null || die "找不到 docker"
  command -v openssl >/dev/null || die "找不到 openssl"
  command -v curl >/dev/null || die "找不到 curl"
  docker info >/dev/null 2>&1 || die "docker 沒在跑，或這個帳號沒有權限"
  require_compose_version
  bundle="$(open_bundle "$spec")" || exit 1
  release_verify_bundle "$bundle" || exit 1
  release_verify_image_catalog "$bundle" || exit 1
  new="$(manifest_value version "$bundle/manifest.txt")"
  [[ -n "$new" ]] || die "清單沒有版本"
  _ops_to="$new"
  # 清單核對之後、建立版本目錄之前再比一次。交接與直接更新都看得到。
  if ! release_version_order_ok "$new" "${old:-}"; then
    exit 1
  fi
  mkdir -p "$root/versions" "$root/state"
  before=""
  dump=""
  stamp=""
  if [[ -n "$old" && "$old" != "none" ]]; then
    if count="$(banner_count "$old_tree")"; then
      banner_gate "$count" || die "已取消。"
    else
      banner_gate 0 || die "已取消。"
    fi
    dump="$root/state/share/backups/pre-update/${old}/db.dump"
    _fail_root="$root"
    _fail_old="$old"
    _fail_new="$new"
    _fail_dump="$dump"
    _fail_before=""
    # 讀不到檔案目錄就在停任何服務之前結束，不走回復。
    assert_share_readable
    # backup_for_update 在命令替換裡。旗標要設在這個 shell，失敗才拉得回上一版。
    _update_failure_restart=1
    info "先停掉會寫入的服務，再做更新前的資料庫備份"
    stamp="$(backup_for_update "$old_tree" "$dump")" || exit 1
    ok "更新前備份：$dump"
    before="$(tr -d '[:space:]' < "${dump}.alembic")"
    _fail_before="$before"
    tag_running_as_version "$old"
  else
    info "這台還沒有在跑的平台，略過公告與更新前備份"
    ensure_first_install_https_port
  fi
  dest="$root/versions/$new"
  rm -rf "$dest"
  unpack_source "$bundle" "$dest"
  ANILA_RETAG_ON_DIE=1
  ANILA_RETAG_VERSION="$old"
  load_bundle_images "$bundle" "$new"
  prepare_tree "$dest" "$root"
  ANILA_RETAG_ON_DIE=0
  switch_current "$root" "$new"
  if ! bring_up "$dest"; then
    append_operations_log update "${old:-none}" "$new" failure
    _ops_done=1
    auto_rollback "$root" "$old" "$new" "$before" "$dump" || true
    die "更新失敗，請看上方的回復結果"
  fi
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  older="$(state_get previous)"
  state_set previous "${old:-none}"
  state_set current "$new"
  state_set updated_at "$now"
  state_set pre_update_dump "$dump"
  state_set alembic_before "$before"
  if [[ -n "$stamp" ]]; then
    state_set backup_at "$stamp"
  fi
  prune_old_images "$old" "$new"
  prune_old_trees "$root" "$old" "$new"
  prune_old_preupdate_backups "$root" "${old:-none}" "${older:-none}"
  install_runner "$bundle" "$root"
  _ops_tree="$dest"
  retry_pending_db_audits "$dest" || true
  record_audit "$dest" update "${old:-none}" "$new" success
  ok "更新完成：${old:-none} → ${new}"
  printf '初始化工作 csp-credential-dirs 已正常結束。它是一次性工作（標籤 anila.oneshot=true），做完就退出；docker ps 只列出運行中的容器。\n'
  printf 'codeserver 與 n8n 的映像已在本機，預設沒有啟動。最後演練才執行：\n'
  printf '  docker compose -f %q -f %q -p %q --profile maint up -d --no-build --pull never codeserver\n' \
    "$(install_root)/current/compose.yaml" "$(install_root)/current/.anila-images.yml" "$(compose_project)"
  printf '  COMPOSE_PROFILES=ops docker compose -f %q -f %q -p %q up -d --no-build --pull never n8n\n' \
    "$(install_root)/current/compose.yaml" "$(install_root)/current/.anila-images.yml" "$(compose_project)"
}

_finish_failed_manual_rollback() {
  local root="$1" running="$2" running_tree="$3" target="$4" why="$5"
  retag_compose_from_version "$running" || true
  if [[ -d "$root/versions/$running" ]]; then
    switch_current "$root" "$running" || true
  fi
  restart_platform "$running_tree"
  _ops_tree="$running_tree"
  _ops_db_ready=1
  append_operations_log rollback "$running" "$target" failure || true
  write_db_audit "$running_tree" rollback "$running" "$target" failure || true
  _ops_done=1
  printf '%s\n' "$why" >&2
  exit 1
}

cmd_rollback() {
  local root cur prev tree running_tree since counts conv msg docs dump
  assert_destructive_allowed
  _ops_action=rollback
  _ops_from=none
  _ops_to=none
  _ops_done=0
  _ops_db_ready=0
  _ops_tree=""
  _update_failure_restart=0
  _arm_exit
  acquire_install_lock
  command -v docker >/dev/null || die "找不到 docker"
  root="$(install_root)"
  cur="$(state_get current)"
  prev="$(state_get previous)"
  _ops_from="${cur:-none}"
  _ops_to="${prev:-none}"
  [[ -n "$cur" && -n "$prev" && "$prev" != "none" ]] || die "沒有上一版可回復"
  [[ -d "$root/versions/$prev" ]] || die "上一版的原始碼目錄不在：versions/${prev}"
  tree="$(current_tree)"
  [[ -n "$tree" ]] || die "找不到目前這版的目錄"
  db_is_up "$tree" || die "資料庫沒在跑，無法計算會消失的筆數，也無法還原。請先讓 csp-db 起來。"
  _ops_tree="$tree"
  _ops_db_ready=1
  retry_pending_db_audits "$tree" || true
  dump="$(state_get pre_update_dump)"
  [[ -f "${dump}.time" ]] || die "備份沒有時間，無法計算會消失的筆數"
  since="$(tr -d '[:space:]' < "${dump}.time")"
  assert_share_readable
  stop_writers "$tree"
  if ! counts="$(created_since_counts "$tree" "$since")"; then
    restart_platform "$tree"
    die "算不出會消失的筆數，已把平台拉起來。"
  fi
  counts="$(printf '%s' "$counts" | tr -d '[:space:]')"
  conv="${counts%%|*}"
  docs="${counts##*|}"
  msg="${counts#*|}"
  msg="${msg%%|*}"
  printf '回復到 %s 會還原更新前的資料庫。\n' "$prev"
  printf '備份之後新增、回復後會消失的筆數：\n'
  printf '  對話 %s\n' "$conv"
  printf '  訊息 %s\n' "$msg"
  printf '  文件 %s\n' "$docs"
  printf '目前版本是 %s。\n' "$cur"
  if ! confirm_rollback_version "$prev" ""; then
    restart_platform "$tree"
    # 資料庫已確認可用。exit 讓結束處理寫 operations.log 與資料庫稽核。
    # return 不會讓 EXIT trap 看到這次的結束碼，管道會變成成功。
    exit 1
  fi
  retag_compose_from_version "$prev"
  if ! stage_share_files "$dump"; then
    _finish_failed_manual_rollback "$root" "$cur" "$tree" "$prev" \
      "檔案快照還原失敗。線上資料庫沒有被換掉，服務已指回 ${cur}。"
  fi
  if ! commit_share_files "$dump"; then
    revert_share_files "$dump" || true
    _finish_failed_manual_rollback "$root" "$cur" "$tree" "$prev" \
      "檔案快照還原失敗。線上資料庫沒有被換掉，服務已指回 ${cur}。"
  fi
  if ! ( restore_dump "$tree" "$dump" ); then
    revert_share_files "$dump" || true
    _finish_failed_manual_rollback "$root" "$cur" "$tree" "$prev" \
      "資料庫還原失敗。線上資料庫沒有被換掉，服務已指回 ${cur}。"
  fi
  # 還原成功才改指標。檔案的上一份先留著，健康檢查過了才清。
  running_tree="$tree"
  switch_current "$root" "$prev"
  tree="$(current_tree)"
  _ops_tree="$tree"
  _ops_db_ready=1
  if ! bring_up "$tree"; then
    revert_preserved_database "$running_tree" || true
    revert_share_files "$dump" || true
    _finish_failed_manual_rollback "$root" "$cur" "$running_tree" "$prev" \
      "回復後健康檢查沒過。已把資料庫、檔案、映像與目前指標切回 ${cur}。"
  fi
  cleanup_share_staging "$dump"
  drop_preserved_database "$tree"
  state_set current "$prev"
  state_set previous none
  state_set updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  record_audit "$tree" rollback "$cur" "$prev" success
  ok "已回復到 ${prev}"
}

cmd_adopt() {
  local tree="${1:-}" phys root base dump stamp
  [[ -n "$tree" && -d "$tree" ]] || die "用法：bash anila-update.sh adopt <安裝根目錄>/versions/<目前版本>"
  _ops_action=adopt
  _ops_from=none
  _ops_to=none
  _ops_done=0
  _ops_db_ready=0
  _ops_tree=""
  _update_failure_restart=0
  _failure_handled=0
  _adopting=0
  _adopting_version=""
  _arm_exit
  phys="$(cd "$tree" && pwd -P)"
  root="$(install_root_phys)"
  if [[ "$(dirname -- "$phys")" != "$root/versions" ]]; then
    die "認領的目錄必須是 ${root}/versions/<版本>，實際是 ${phys}。"
  fi
  base="$(basename -- "$phys")"
  [[ -f "$phys/compose.yaml" ]] || die "這個版本目錄沒有 compose.yaml"
  refuse_if_anchor_mismatch
  if [[ -n "$(state_get compose_project || true)" && -n "$(state_get current || true)" && "$(state_get current)" != "none" ]]; then
    die "已有安裝記錄（目前 $(state_get current)）。不必再認領。"
  fi
  if [[ "$(compose_project)" == "anila" ]]; then
    if git -C "$HERE" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
      || [[ "$HERE" == */scripts/release ]]; then
      die "拒絕操作：從工作樹對 compose 專案 anila 停服務或動資料庫。這會打到這台正在跑的平台。"
    fi
    if ! install_root_is_real_anchor; then
      die "拒絕操作：compose 專案 anila 只允許裝在 /opt/anila，或腳本寫在安裝包外面的記錄。"
    fi
  fi
  acquire_install_lock
  if ! existing_compose_stack; then
    die "拒絕操作：沒有名稱像 $(compose_project) 的 compose 專案或 volume。這是第一次安裝，請解開出貨包後直接更新。"
  fi
  _adopting=1
  _adopting_version="$base"
  _ops_to="$base"
  db_is_up "$phys" || die "資料庫沒有回應，不能在沒有備份的情況下認領。"
  _ops_tree="$phys"
  _ops_db_ready=1
  _fail_root="$root"
  _fail_old="$base"
  _fail_new="$base"
  _update_failure_restart=1
  dump="$root/state/share/backups/pre-update/${base}/db.dump"
  assert_share_readable
  stamp="$(backup_for_update "$phys" "$dump")" || exit 1
  state_set install_root "$root"
  state_set compose_project "$(compose_project)"
  state_set current "$base"
  state_set previous none
  state_set updated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  state_set pre_update_dump "$dump"
  state_set alembic_before "$(tr -d '[:space:]' < "${dump}.alembic")"
  state_set backup_at "$stamp"
  if [[ "$(compose_project)" == "anila" ]]; then
    write_install_anchor "$root" "anila"
  fi
  switch_current "$root" "$base"
  _adopting=0
  _update_failure_restart=0
  prepare_tree "$phys" "$root" || die "已寫下安裝記錄，但站台目錄沒準備好。"
  bring_up "$phys" || die "已寫下安裝記錄，但服務沒有拉起來。"
  record_audit "$phys" adopt none "$base" success
  ok "已認領 $(compose_project)，目前版本 ${base}。備份在 ${dump}"
}

main() {
  case "${1:-}" in
    rollback) cmd_rollback ;;
    adopt) cmd_adopt "${2:-}" ;;
    ""|-h|--help|help)
      printf '用法：bash anila-update.sh <出貨包目錄或 tar.gz>\n'
      printf '      bash anila-update.sh rollback\n'
      printf '      bash anila-update.sh adopt <versions/版本目錄>\n'
      ;;
    *) cmd_update "$1" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
