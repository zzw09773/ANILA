#!/usr/bin/env bash
# Isolated PATH-stub tests for deploy-prod.sh cmd_verify / deploy error
# propagation. Never calls a real docker daemon or network curl.
set -euo pipefail

REAL_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy-prod.sh"
if [[ ! -f "$REAL_SCRIPT" ]]; then
  echo "missing $REAL_SCRIPT" >&2
  exit 2
fi

PASS=0
FAIL=0
assert_eq() {
  local got="$1" want="$2" msg="$3"
  if [[ "$got" == "$want" ]]; then
    echo "  PASS $msg (exit=$got)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL $msg (got=$got want=$want)"
    FAIL=$((FAIL + 1))
  fi
}

write_stubs() {
  local bindir="$1"
  mkdir -p "$bindir"

  cat > "$bindir/git" <<'EOS'
#!/usr/bin/env bash
echo "prod-public-passwd"
exit 0
EOS

  cat > "$bindir/openssl" <<'EOS'
#!/usr/bin/env bash
out=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-out" ]]; then
    out="$arg"
  fi
  prev="$arg"
done
if [[ -n "$out" ]]; then
  mkdir -p "$(dirname "$out")"
  : > "$out"
fi
exit 0
EOS

  cat > "$bindir/curl" <<'EOS'
#!/usr/bin/env bash
max=5
url=""
prev=""
for arg in "$@"; do
  case "$arg" in
    http://*|https://*) url="$arg" ;;
  esac
  if [[ "$prev" == "--max-time" || "$prev" == "-m" ]]; then
    max="$arg"
  fi
  prev="$arg"
done
sleep_for="${CURL_SLEEP:-0}"
if [ "$sleep_for" -gt "$max" ] 2>/dev/null; then
  echo "000"
  exit 28
fi
case "$url" in
  https://localhost/health*)
    echo "${VERIFY_NGINX:-200}"
    if [[ "${VERIFY_NGINX:-200}" == "200" ]]; then
      exit 0
    fi
    exit 22
    ;;
  *)
    echo "200"
    exit 0
    ;;
esac
EOS

  cat > "$bindir/docker" <<'EOS'
#!/usr/bin/env bash
echo "docker $*" >> "${DOCKER_LOG:-/tmp/anila-f6-docker.log}"
joined="$*"
if [[ "$1" == "compose" && "$2" == "exec" ]]; then
  if [[ "$joined" == *"localhost:8000/health"* ]]; then
    [[ "${VERIFY_CSP:-ok}" == "ok" ]] && exit 0
    exit 1
  fi
  if [[ "$joined" == *"localhost:8100/health"* ]]; then
    [[ "${VERIFY_STUDIO:-ok}" == "ok" ]] && exit 0
    exit 1
  fi
  if [[ "$joined" == *"revocations"* ]]; then
    echo "${VERIFY_REVOKE:-200}"
    [[ "${VERIFY_REVOKE:-200}" == "200" ]] && exit 0
    exit 1
  fi
  exit 0
fi
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
  echo "Up (healthy)"
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  echo "healthy"
  exit 0
fi
if [[ "$1" == "ps" ]]; then
  echo "anila-nginx"
  exit 0
fi
exit 0
EOS

  chmod +x "$bindir/"*
}

make_repo() {
  local root="$1"
  mkdir -p "$root/bin" "$root/repo/infra/deployment/scripts" \
    "$root/repo/share/uploads/flux" "$root/repo/share/pki"
  cp "$REAL_SCRIPT" "$root/repo/infra/deployment/scripts/deploy-prod.sh"
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$root/repo/infra/deployment/scripts/fix-runtime-ownership.sh"
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$root/repo/infra/deployment/scripts/check-departments.sh"
  chmod +x "$root/repo/infra/deployment/scripts/"*.sh
  write_stubs "$root/bin"
}

run_cmd() {
  local root="$1"
  local subcmd="$2"
  local stubpath="$root/bin"
  [[ "$(command -v docker)" == "$stubpath/docker" ]] || {
    # force the child PATH; do not inherit a hashed real docker/curl
    true
  }
  env -i \
    PATH="$stubpath:/bin:/usr/bin" \
    HOME="$root" \
    CSP_SERVICE_TOKEN="test-token-not-a-secret" \
    INTERNAL_PLATFORM_API_KEY="test-internal-key" \
    SECRET_KEY="test-secret-key-value" \
    ANILA_ALLOW_DEV_SECRET="0" \
    ANILA_ENV="production" \
    DOCKER_LOG="$root/docker.log" \
    VERIFY_NGINX="${VERIFY_NGINX:-200}" \
    VERIFY_CSP="${VERIFY_CSP:-ok}" \
    VERIFY_STUDIO="${VERIFY_STUDIO:-ok}" \
    VERIFY_REVOKE="${VERIFY_REVOKE:-200}" \
    CURL_SLEEP="${CURL_SLEEP:-0}" \
    bash -c "cd '$root/repo' && bash infra/deployment/scripts/deploy-prod.sh '$subcmd'"
}

HARNESS_ROOT="${ANILA_F6_HARNESS_ROOT:-/home/c1147259/.codex/visualizations/2026/09/08/01a07ff6-13a6-7330-83a7-8cb219064b1f/f6-harness}"
mkdir -p "$HARNESS_ROOT"
BASE="$(mktemp -d "$HARNESS_ROOT/run.XXXXXX")"
trap 'rm -rf "$BASE"' EXIT

run_case() {
  local name="$1" want="$2" subcmd="$3"
  echo "== $name =="
  local H="$BASE/$name"
  make_repo "$H"
  : > "$H/docker.log"
  set +e
  run_cmd "$H" "$subcmd" >/tmp/anila-f6-last.out 2>&1
  local rc=$?
  set -e
  tail -n 12 /tmp/anila-f6-last.out
  assert_eq "$rc" "$want" "$name"
}

VERIFY_NGINX=200 VERIFY_CSP=ok VERIFY_STUDIO=ok VERIFY_REVOKE=200 run_case "verify-all-success" 0 verify
VERIFY_NGINX=000 VERIFY_CSP=ok VERIFY_STUDIO=ok VERIFY_REVOKE=200 run_case "verify-nginx-fail" 1 verify
VERIFY_NGINX=200 VERIFY_CSP=bad VERIFY_STUDIO=ok VERIFY_REVOKE=200 run_case "verify-csp-fail" 1 verify
VERIFY_NGINX=200 VERIFY_CSP=ok VERIFY_STUDIO=bad VERIFY_REVOKE=200 run_case "verify-studio-fail" 1 verify
VERIFY_NGINX=200 VERIFY_CSP=ok VERIFY_STUDIO=ok VERIFY_REVOKE=503 run_case "verify-revoke-fail" 1 verify
VERIFY_NGINX=500 VERIFY_CSP=bad VERIFY_STUDIO=bad VERIFY_REVOKE=fail run_case "verify-all-fail" 1 verify
CURL_SLEEP=10 VERIFY_NGINX=200 VERIFY_CSP=ok VERIFY_STUDIO=ok VERIFY_REVOKE=200 run_case "verify-timeout" 1 verify
VERIFY_NGINX=000 VERIFY_CSP=ok VERIFY_STUDIO=ok VERIFY_REVOKE=200 run_case "deploy-verify-fail" 1 deploy

echo "== harness safety =="
if grep -E '/usr/bin/docker|/usr/local/bin/docker' "$BASE"/deploy-verify-fail/docker.log >/dev/null 2>&1; then
  echo "  FAIL harness leaked a real docker path"
  FAIL=$((FAIL + 1))
else
  echo "  PASS harness docker log has no real daemon path"
  PASS=$((PASS + 1))
fi

echo
echo "passed=$PASS failed=$FAIL"
if (( FAIL > 0 )); then
  exit 1
fi
exit 0
