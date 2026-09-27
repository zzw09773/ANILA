#!/usr/bin/env bash
# 正式部署必須拒絕開發用登入與測試 CA。訊息只點鍵名，不印值。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
GUARD="$ROOT/infra/deployment/scripts/prod-env-guard.sh"
# shellcheck source=../prod-env-guard.sh
source "$GUARD"

fail() { echo "FAIL: $*" >&2; exit 1; }

CANARY='super-secret-dev-ca-path-CANARY'

assert_refuses() {
  local key="$1"
  local file="$2"
  local err
  err="$(mktemp)"
  if prod_env_refuse "$file" >"$err" 2>&1; then
    cat "$err" >&2
    rm -f "$err"
    fail "$key should have been refused"
  fi
  grep -q "$key" "$err" || { cat "$err" >&2; rm -f "$err"; fail "$key name missing from the refusal"; }
  if grep -q "$CANARY" "$err"; then
    rm -f "$err"
    fail "$key refusal printed a value"
  fi
  rm -f "$err"
}

good="$(mktemp)"
cat >"$good" <<'EOF'
ANILA_ALLOW_DEV_SECRET=0
CARD_DEV_TRUST_TEST_CA=0
CARD_CA_BUNDLE_PATH=
ANILA_AUTH_MODE=card-only
SECRET_KEY=not-printed
EOF
unset ANILA_ALLOW_DEV_SECRET CARD_DEV_TRUST_TEST_CA CARD_CA_BUNDLE_PATH ANILA_AUTH_MODE
prod_env_refuse "$good" || fail "clean file was refused"
rm -f "$good"

# 缺 ANILA_AUTH_MODE：compose 預設就是 card-only，不拒絕。
absent="$(mktemp)"
printf 'ANILA_ALLOW_DEV_SECRET=0\n' >"$absent"
prod_env_refuse "$absent" || fail "absent auth mode was refused"
rm -f "$absent"

bad_dev="$(mktemp)"
cat >"$bad_dev" <<EOF
ANILA_ALLOW_DEV_SECRET="1"
CARD_CA_BUNDLE_PATH=/tmp/${CANARY}/dev-card-ca/bundle.pem
EOF
assert_refuses ANILA_ALLOW_DEV_SECRET "$bad_dev"
rm -f "$bad_dev"

bad_card="$(mktemp)"
printf 'CARD_DEV_TRUST_TEST_CA=1\nANILA_AUTH_MODE=card-only\n' >"$bad_card"
assert_refuses CARD_DEV_TRUST_TEST_CA "$bad_card"
rm -f "$bad_card"

bad_path="$(mktemp)"
printf 'CARD_CA_BUNDLE_PATH=/opt/%s/dev_ca_bundle.pem\nANILA_AUTH_MODE=card-only\n' "$CANARY" >"$bad_path"
assert_refuses CARD_CA_BUNDLE_PATH "$bad_path"
rm -f "$bad_path"

bad_mode="$(mktemp)"
printf 'ANILA_AUTH_MODE=password\n' >"$bad_mode"
assert_refuses ANILA_AUTH_MODE "$bad_mode"
rm -f "$bad_mode"

# shell 蓋過檔案：compose 用 shell 的值，也要拒絕，而且不印那個值。
shellfile="$(mktemp)"
printf 'ANILA_AUTH_MODE=card-only\n' >"$shellfile"
export ANILA_ALLOW_DEV_SECRET=1
assert_refuses ANILA_ALLOW_DEV_SECRET "$shellfile"
unset ANILA_ALLOW_DEV_SECRET
rm -f "$shellfile"

echo "ok"
