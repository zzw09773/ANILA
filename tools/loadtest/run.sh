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

_remove_created_keys() {
  local jwt="$1"
  local created_list="$2"
  if [ -z "$created_list" ] || [ ! -f "$created_list" ]; then
    return 0
  fi
  local path
  while IFS= read -r path || [ -n "${path:-}" ]; do
    case "$path" in
      "$jwt"/jwt-private.pem|"$jwt"/jwt-public.pem)
        rm -f -- "$path" || true
        ;;
      "")
        ;;
      *)
        echo "loadtest: 拒絕刪除金鑰路徑「${path}」" >&2
        ;;
    esac
  done < "$created_list"
  rm -f -- "$created_list" || true
}

install_loadtest_jwt() {
  local jwt="$1"
  local created_list="${2:-}"
  mkdir -p "$jwt"
  if [ ! -f "$jwt/jwt-private.pem" ]; then
    _note_created_key "$created_list" "$jwt/jwt-private.pem"
    _note_created_key "$created_list" "$jwt/jwt-public.pem"
    (
      umask 077
      openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$jwt/jwt-private.pem"
      openssl pkey -in "$jwt/jwt-private.pem" -pubout -out "$jwt/jwt-public.pem"
    )
    chmod 644 "$jwt/jwt-public.pem"
  fi
  # 0600 before chown. A failed chown must not widen the private key.
  chmod 600 "$jwt/jwt-private.pem"
  local -a targets=("$jwt/jwt-private.pem")
  if [ -f "$jwt/jwt-public.pem" ]; then
    targets+=("$jwt/jwt-public.pem")
  fi
  if ! chown 10001:10001 "${targets[@]}"; then
    echo "loadtest: chown 10001:10001 失敗，JWT 私鑰維持 0600，不會改成 0644。" >&2
    exit 1
  fi
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
