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

# 退役的共用權杖：空的或沒有這一行可以過；有值就拒絕，而且不印值。
unset CSP_SERVICE_TOKEN CSP_BOOTSTRAP_TOKEN
blank="$(mktemp)"
printf 'CSP_SERVICE_TOKEN=\nCSP_BOOTSTRAP_TOKEN=""\nANILA_AUTH_MODE=card-only\n' >"$blank"
prod_env_refuse "$blank" || fail "blank retired tokens were refused"
rm -f "$blank"
export CSP_SERVICE_TOKEN=""
export CSP_BOOTSTRAP_TOKEN=""
empty_shell="$(mktemp)"
printf 'ANILA_AUTH_MODE=card-only\n' >"$empty_shell"
prod_env_refuse "$empty_shell" || fail "empty shell retired tokens were refused"
unset CSP_SERVICE_TOKEN CSP_BOOTSTRAP_TOKEN
rm -f "$empty_shell"

retired="$(mktemp)"
printf 'CSP_SERVICE_TOKEN=%s\nANILA_AUTH_MODE=card-only\n' "$CANARY" >"$retired"
assert_refuses CSP_SERVICE_TOKEN "$retired"
grep -q '已退役' <<<"$(prod_env_refuse "$retired" 2>&1 || true)" || fail "retired message missing"
rm -f "$retired"

retired_boot="$(mktemp)"
printf 'CSP_BOOTSTRAP_TOKEN=%s\n' "$CANARY" >"$retired_boot"
assert_refuses CSP_BOOTSTRAP_TOKEN "$retired_boot"
rm -f "$retired_boot"

export CSP_SERVICE_TOKEN="$CANARY"
shell_retired="$(mktemp)"
printf 'ANILA_AUTH_MODE=card-only\n' >"$shell_retired"
assert_refuses CSP_SERVICE_TOKEN "$shell_retired"
unset CSP_SERVICE_TOKEN
rm -f "$shell_retired"

# 行內註解照 compose 的讀法（已用 docker compose config 對過）：
# 引號值只取到結束引號；沒加引號的值，值後的「空白 + #」起是註解；
# 開頭就是 # 仍是值（KEY= #abc 讀成 #abc）。
commented="$(mktemp)"
printf 'CSP_SERVICE_TOKEN="" # retired\nANILA_AUTH_MODE=card-only # note\n' >"$commented"
prod_env_refuse "$commented" 2>/dev/null || fail "inline comment counted as a value"
printf "ANILA_AUTH_MODE='card-only' # note\n" >"$commented"
prod_env_refuse "$commented" 2>/dev/null || fail "comment after a quoted value counted"
printf 'CSP_SERVICE_TOKEN=%s # retired\n' "$CANARY" >"$commented"
assert_refuses CSP_SERVICE_TOKEN "$commented"
printf 'CSP_SERVICE_TOKEN=   # retired\n' >"$commented"
assert_refuses CSP_SERVICE_TOKEN "$commented"
printf 'ANILA_AUTH_MODE=mixed # note\n' >"$commented"
assert_refuses ANILA_AUTH_MODE "$commented"
printf 'ANILA_ALLOW_DEV_SECRET="1" # dev\n' >"$commented"
assert_refuses ANILA_ALLOW_DEV_SECRET "$commented"
printf 'CARD_DEV_TRUST_TEST_CA="1" # dev\n' >"$commented"
assert_refuses CARD_DEV_TRUST_TEST_CA "$commented"
# tab 後的 # 不是註解：compose 讀到的是 "card-only<tab># c"，不是 card-only。
printf 'ANILA_AUTH_MODE=card-only\t# c\n' >"$commented"
assert_refuses ANILA_AUTH_MODE "$commented"
# compose 也吃 `KEY = v`、`KEY: v`，會展開 ${VAR}，引號內空白原樣保留，行尾 CR 去掉。
printf 'ANILA_ALLOW_DEV_SECRET = 1\n' >"$commented"
assert_refuses ANILA_ALLOW_DEV_SECRET "$commented"
printf 'CARD_DEV_TRUST_TEST_CA: 1\n' >"$commented"
assert_refuses CARD_DEV_TRUST_TEST_CA "$commented"
printf 'FLAG=1\nANILA_ALLOW_DEV_SECRET=${FLAG}\n' >"$commented"
assert_refuses ANILA_ALLOW_DEV_SECRET "$commented"
grep -q '變數展開' <<<"$(prod_env_refuse "$commented" 2>&1 || true)" || fail "dynamic value message missing"
printf 'ANILA_ALLOW_DEV_SECRET="${FLAG}"\n' >"$commented"
assert_refuses ANILA_ALLOW_DEV_SECRET "$commented"
printf 'CARD_CA_BUNDLE_PATH=/certs/${DIR}/ca.pem\n' >"$commented"
assert_refuses CARD_CA_BUNDLE_PATH "$commented"
printf "ANILA_ALLOW_DEV_SECRET='\${FLAG}'\n" >"$commented"
prod_env_refuse "$commented" 2>/dev/null || fail "single-quoted literal treated as expansion"
printf 'ANILA_AUTH_MODE=" card-only "\n' >"$commented"
assert_refuses ANILA_AUTH_MODE "$commented"
printf 'CSP_SERVICE_TOKEN=" "\n' >"$commented"
assert_refuses CSP_SERVICE_TOKEN "$commented"
# 引號內的反斜線與跨行值，compose 的讀法和逐行解析不同，一律拒絕。
printf "CARD_CA_BUNDLE_PATH='safe\\\\'/certs/dev_ca.pem'\n" >"$commented"
assert_refuses CARD_CA_BUNDLE_PATH "$commented"
printf "CARD_CA_BUNDLE_PATH='\n/certs/dev_ca.pem'\n" >"$commented"
assert_refuses CARD_CA_BUNDLE_PATH "$commented"
printf 'ANILA_AUTH_MODE="card-only\\"\n' >"$commented"
assert_refuses ANILA_AUTH_MODE "$commented"
printf 'ANILA_AUTH_MODE=card-only\r\nCSP_SERVICE_TOKEN=\r\n' >"$commented"
prod_env_refuse "$commented" 2>/dev/null || fail "CRLF line endings refused"
rm -f "$commented"

echo "ok"
