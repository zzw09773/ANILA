#!/usr/bin/env bash
# One command: isolated dev stack + mock model + the four concurrency levels.
# Does not start or recreate the live project (anila / nginx :443).
# EXIT removes this project's volumes and JWT keys this run created. Keys
# that were already in the directory stay. It does not touch anila-platform-dev.
set -euo pipefail

PROJECT=anila-loadtest
MOCK=anila-loadtest-mock

_note_created_key() {
  local created_list="$1"
  local path="$2"
  if [ -z "$created_list" ]; then
    return 0
  fi
  printf '%s\n' "$path" >> "$created_list"
}

# 刪檔只需要目錄的寫入與進入權，不需要擁有檔案本身，也不讀私鑰。
# 目錄是 0770、沒有 sticky bit：擁有者 10001（容器），群組是執行者的 gid，
# 所以 chown 之後執行者仍能 unlink。私鑰維持 0600、擁有者 10001，
# 群組讀不到內容。下次再跑時目錄已不屬於執行者，chmod 會失敗；
# 模式與擁有者已經正確就繼續，不會把權限放寬。
_mode_matches() {
  local path="$1"
  local want="$2"
  local got
  got="$(stat -c '%a' "$path" 2>/dev/null || true)"
  got="${got#0}"
  want="${want#0}"
  [ -n "$got" ] && [ "$got" = "$want" ]
}

_owned_by() {
  local path="$1"
  local uid="$2"
  local gid="$3"
  local got_uid got_gid
  got_uid="$(stat -c '%u' "$path" 2>/dev/null || true)"
  got_gid="$(stat -c '%g' "$path" 2>/dev/null || true)"
  [ "$got_uid" = "$uid" ] && [ "$got_gid" = "$gid" ]
}

_ensure_mode() {
  local path="$1"
  local mode="$2"
  local label="$3"
  if chmod "$mode" "$path"; then
    return 0
  fi
  if _mode_matches "$path" "$mode"; then
    return 0
  fi
  echo "loadtest: 無法把${label}設成 ${mode}。不會放寬權限。" >&2
  exit 1
}

_chown_container_owner() {
  local gid="$1"
  shift
  if chown "10001:${gid}" "$@"; then
    return 0
  fi
  local path
  for path in "$@"; do
    if ! _owned_by "$path" 10001 "$gid"; then
      echo "loadtest: chown 10001:${gid} 失敗，JWT 私鑰維持 0600，目錄維持 0770，不會放寬權限。" >&2
      exit 1
    fi
  done
}

_remove_created_keys() {
  local jwt="$1"
  local created_list="$2"
  if [ -z "$created_list" ] || [ ! -f "$created_list" ]; then
    return 0
  fi
  local path
  local private="$jwt/jwt-private.pem"
  local public="$jwt/jwt-public.pem"
  while IFS= read -r path || [ -n "${path:-}" ]; do
    if [ -z "$path" ]; then
      continue
    fi
    # 字串相等。樣式比對會把路徑裡的 * ? [ 當成萬用字元。
    if [ "$path" = "$private" ] || [ "$path" = "$public" ]; then
      rm -f -- "$path" || true
    else
      echo "loadtest: 拒絕刪除金鑰路徑「${path}」" >&2
    fi
  done < "$created_list"
  rm -f -- "$created_list" || true
}

install_loadtest_jwt() {
  local jwt="$1"
  local created_list="${2:-}"
  local gid
  gid="$(id -g)"
  mkdir -p "$jwt"
  # 先設模式（還擁有目錄時 chmod 才成功），再產生金鑰，最後才交給 10001。
  _ensure_mode "$jwt" 0770 "金鑰目錄"
  local private_is_new=0
  if [ ! -f "$jwt/jwt-private.pem" ]; then
    (
      umask 077
      openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$jwt/jwt-private.pem"
    )
    _note_created_key "$created_list" "$jwt/jwt-private.pem"
    private_is_new=1
  fi
  # 私鑰是這次新產生的話，舊公鑰要重算，否則兩把對不上。
  # 公鑰本來就不在時也補。清單只記這次新建的檔，蓋掉的舊公鑰不刪。
  if [ "$private_is_new" -eq 1 ] || [ ! -f "$jwt/jwt-public.pem" ]; then
    local public_is_new=0
    if [ ! -f "$jwt/jwt-public.pem" ]; then
      public_is_new=1
    fi
    (
      umask 077
      openssl pkey -in "$jwt/jwt-private.pem" -pubout -out "$jwt/jwt-public.pem"
    )
    if [ "$public_is_new" -eq 1 ]; then
      _note_created_key "$created_list" "$jwt/jwt-public.pem"
    fi
  fi
  _ensure_mode "$jwt/jwt-private.pem" 600 "JWT 私鑰"
  local -a targets=("$jwt" "$jwt/jwt-private.pem")
  if [ -f "$jwt/jwt-public.pem" ]; then
    _ensure_mode "$jwt/jwt-public.pem" 644 "JWT 公鑰"
    targets+=("$jwt/jwt-public.pem")
  fi
  _chown_container_owner "$gid" "${targets[@]}"
}

cleanup_loadtest() {
  local root="$1"
  local jwt="$2"
  local created_list="${3:-}"
  docker compose -p "$MOCK" -f "$root/tools/loadtest/compose.yml" down --volumes --remove-orphans || true
  docker compose -p "$PROJECT" -f "$root/compose.dev.yaml" -f "$root/tools/loadtest/stack.override.yml" down --volumes --remove-orphans || true
  if [ -z "$jwt" ] || [ "$jwt" = "/" ]; then
    echo "loadtest: 拒絕刪除金鑰路徑「${jwt:-}」" >&2
    return 1
  fi
  _remove_created_keys "$jwt" "$created_list"
  if [ -d "$jwt" ]; then
    rmdir -- "$jwt" 2>/dev/null || true
  fi
}

main() {
  local root
  root="$(cd "$(dirname "$0")/../.." && pwd)"
  cd "$root"
  if ulimit -n 65535 2>/dev/null; then
    :
  fi
  local jwt="$root/tools/loadtest/.jwt"
  local created
  created="$(mktemp)"
  # The EXIT trap runs after this function's locals are gone, so bake the paths in.
  # shellcheck disable=SC2064
  trap "cleanup_loadtest $(printf '%q' "$root") $(printf '%q' "$jwt") $(printf '%q' "$created")" EXIT
  install_loadtest_jwt "$jwt" "$created"
  docker compose -p "$PROJECT" -f compose.dev.yaml -f tools/loadtest/stack.override.yml up -d --build
  docker compose -p "$MOCK" -f tools/loadtest/compose.yml up -d
  # Login hits nginx as soon as the script starts. Wait until CSP is
  # actually healthy; "started" still returns 502.
  local health=""
  for _ in $(seq 1 90); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "${PROJECT}-csp-1" 2>/dev/null || true)
    if [ "$health" = "healthy" ]; then
      break
    fi
    sleep 2
  done
  if [ "$health" != "healthy" ]; then
    echo "csp did not become healthy" >&2
    exit 1
  fi
  set +e
  python3 tools/loadtest/generate.py "$@"
  local status=$?
  set -e
  exit "$status"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
