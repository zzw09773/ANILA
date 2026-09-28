#!/usr/bin/env bash
# One command: isolated dev stack + mock model + the four concurrency levels.
# Does not start or recreate the live project (anila / nginx :443).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if ulimit -n 65535 2>/dev/null; then
  :
fi
jwt="$ROOT/tools/loadtest/.jwt"
mkdir -p "$jwt"
if [ ! -f "$jwt/jwt-private.pem" ]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$jwt/jwt-private.pem"
  openssl pkey -in "$jwt/jwt-private.pem" -pubout -out "$jwt/jwt-public.pem"
  chmod 640 "$jwt/jwt-private.pem"
  chmod 644 "$jwt/jwt-public.pem"
  chown 10001:10001 "$jwt/jwt-private.pem" "$jwt/jwt-public.pem" 2>/dev/null || chmod 644 "$jwt/jwt-private.pem"
fi
# Own project, network and volumes. compose.dev.yaml's name is the daily
# dev stack; -f alone would recreate it. The trap downs this project even
# when the run is interrupted. It does not remove volumes and does not
# touch the live project (anila / nginx :443) or anila-platform-dev.
PROJECT=anila-loadtest
MOCK=anila-loadtest-mock
cleanup() {
  docker compose -p "$MOCK" -f tools/loadtest/compose.yml down --remove-orphans || true
  docker compose -p "$PROJECT" -f compose.dev.yaml -f tools/loadtest/stack.override.yml down --remove-orphans || true
}
trap cleanup EXIT
docker compose -p "$PROJECT" -f compose.dev.yaml -f tools/loadtest/stack.override.yml up -d --build
docker compose -p "$MOCK" -f tools/loadtest/compose.yml up -d
# Login hits nginx as soon as the script starts. Wait until CSP is
# actually healthy; "started" still returns 502.
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
status=$?
set -e
exit "$status"
