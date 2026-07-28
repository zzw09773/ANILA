#!/usr/bin/env bash
# router-chain-e2e.sh —「瀏覽器 → Router 聊天」主路徑防回歸 E2E。
#
# 動機:
#   R3 formal authority 上線後,UI 必須走 CSP `/v1/chat/completions`(由 CSP
#   注入 X-ANILA-Router-Context),不可直打 nginx `/router/`。2026-07-24 才以
#   人工除錯修復(commit d02b882);本腳本把當日已驗證流程固化成可重跑斷言,
#   鎖住正向串流與負向 401 契約,防止同型回歸。
#
# 前置:
#   1. 本機 dev stack 已起(compose.dev.yaml / project anila-platform-dev;
#      nginx HTTPS 預設 https://localhost:8443)。
#   2. 已跑過 infra/deployment/scripts/router-chain-bootstrap.sh,且依其提示
#      recreate 過 router/csp(service token 與 sentinel 就緒)。
#   3. repo root `.env` 含可登入的 ADMIN_PASSWORD。
#
# 用法:
#   infra/deployment/scripts/router-chain-e2e.sh
# 環境變數(選填):
#   BASE              平台入口(預設 https://localhost:8443)
#   ADMIN_USERNAME    登入帳號(預設 admin)
#   ADMIN_PASSWORD    覆寫 .env 的 ADMIN_PASSWORD(通常不需設)
#   CHAT_PROMPT       正向聊天 user 訊息(預設簡短 ping)
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

BASE="${BASE:-https://localhost:8443}"
BASE="${BASE%/}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
CHAT_PROMPT="${CHAT_PROMPT:-請只回覆一個字：ok}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: 需要 host 端 python3 以讀 .env 與解析 JSON" >&2
  exit 2
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: 需要 curl" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/router-chain-e2e.XXXXXX")"
COOKIE_JAR="$TMP_DIR/cookies.txt"
BODY_FILE="$TMP_DIR/body.txt"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# 失敗時印步驟名、HTTP code、回應前 300 字元後非零退出。
fail_step() {
  local step="$1" code="$2" body="${3-}"
  local preview
  preview="$(printf '%s' "$body" | head -c 300)"
  printf 'FAIL step=%s http=%s\n' "$step" "$code" >&2
  printf 'response (first 300 chars): %s\n' "$preview" >&2
  exit 1
}

# 從 Netscape cookie jar 取出 CSRF(相容 __Host- / anila_dev_ 兩種姿態)。
# 票據口語的 anila_csrf 對應正式 cookie 名 __Host-anila_csrf(或 dev 的 anila_dev_csrf)。
extract_csrf() {
  local jar="$1"
  "$PYTHON_BIN" - "$jar" <<'PY'
import sys
from pathlib import Path

jar = Path(sys.argv[1])
if not jar.is_file():
    raise SystemExit(1)
preferred = ("__Host-anila_csrf", "anila_dev_csrf", "anila_csrf")
found = {}
for line in jar.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line or line.startswith("#"):
        continue
    parts = line.split("\t")
    if len(parts) < 7:
        continue
    name, value = parts[-2], parts[-1]
    if name in preferred and value:
        found[name] = value
for name in preferred:
    if name in found:
        print(found[name], end="")
        raise SystemExit(0)
raise SystemExit(1)
PY
}

# POST JSON;寫 body 到 BODY_FILE,stdout 印 HTTP code。Cookie jar 可選讀寫。
curl_json_post() {
  local url="$1" payload="$2"
  shift 2
  # 其餘參數原樣傳給 curl(例如 -b/-c、-H、--max-time)。
  curl -sk -o "$BODY_FILE" -w '%{http_code}' \
    -X POST "$url" \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$@"
}

if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: 找不到 $ENV_FILE,且未設 ADMIN_PASSWORD" >&2
    exit 2
  fi
  ADMIN_PASSWORD="$(
    ENV_FILE="$ENV_FILE" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

for line in Path(os.environ["ENV_FILE"]).read_text(encoding="utf-8", errors="replace").splitlines():
    s = line.strip()
    if s.startswith("ADMIN_PASSWORD=") and not s.startswith("#"):
        print(s.split("=", 1)[1], end="")
        break
else:
    raise SystemExit(1)
PY
  )" || {
    echo "ERROR: $ENV_FILE 未含 ADMIN_PASSWORD" >&2
    exit 2
  }
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "ERROR: ADMIN_PASSWORD 為空" >&2
  exit 2
fi

LOGIN_PAYLOAD="$(
  ADMIN_USERNAME="$ADMIN_USERNAME" ADMIN_PASSWORD="$ADMIN_PASSWORD" "$PYTHON_BIN" - <<'PY'
import json, os
print(json.dumps({
    "username": os.environ["ADMIN_USERNAME"],
    "password": os.environ["ADMIN_PASSWORD"],
}, ensure_ascii=False, separators=(",", ":")))
PY
)"
# 密碼已進 payload,清掉 shell 變數避免後續洩漏到 set -x / 錯誤訊息。
unset ADMIN_PASSWORD

echo "== Router chain E2E =="
echo "  BASE=$BASE"

# ---------------------------------------------------------------------------
# 1) login — cookie jar 收 session + CSRF
# ---------------------------------------------------------------------------
STEP="1_login"
CODE="$(curl_json_post "$BASE/api/auth/login" "$LOGIN_PAYLOAD" -c "$COOKIE_JAR")"
BODY="$(cat "$BODY_FILE")"
if [[ "$CODE" != "200" ]]; then
  fail_step "$STEP" "$CODE" "$BODY"
fi
if ! CSRF_TOKEN="$(extract_csrf "$COOKIE_JAR")"; then
  fail_step "$STEP" "$CODE" "login ok but CSRF cookie missing in jar; body=${BODY}"
fi
echo "  OK $STEP (csrf present)"

# ---------------------------------------------------------------------------
# 2) create conversation
# ---------------------------------------------------------------------------
STEP="2_create_conversation"
CONV_PAYLOAD='{"title":"router-e2e"}'
CODE="$(curl_json_post "$BASE/api/conversations" "$CONV_PAYLOAD" \
  -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN")"
BODY="$(cat "$BODY_FILE")"
if [[ "$CODE" != "200" && "$CODE" != "201" ]]; then
  fail_step "$STEP" "$CODE" "$BODY"
fi
CONV_ID="$(
  BODY="$BODY" "$PYTHON_BIN" - <<'PY'
import json, os, sys
try:
    data = json.loads(os.environ["BODY"])
except (KeyError, ValueError, TypeError):
    raise SystemExit(1)
cid = data.get("id")
if cid is None:
    raise SystemExit(1)
print(cid)
PY
)" || fail_step "$STEP" "$CODE" "$BODY"
echo "  OK $STEP (conversation_id=$CONV_ID)"

# ---------------------------------------------------------------------------
# 3) create task
# ---------------------------------------------------------------------------
STEP="3_create_task"
TASK_PAYLOAD="$(
  CONV_ID="$CONV_ID" "$PYTHON_BIN" - <<'PY'
import json, os
print(json.dumps({
    "title": "router-e2e",
    "task_type": "query",
    "source_scope": "none",
    "conversation_id": int(os.environ["CONV_ID"])
        if str(os.environ["CONV_ID"]).isdigit()
        else os.environ["CONV_ID"],
}, ensure_ascii=False, separators=(",", ":")))
PY
)"
CODE="$(curl_json_post "$BASE/api/tasks" "$TASK_PAYLOAD" \
  -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN")"
BODY="$(cat "$BODY_FILE")"
if [[ "$CODE" != "200" && "$CODE" != "201" ]]; then
  fail_step "$STEP" "$CODE" "$BODY"
fi
TASK_ID="$(
  BODY="$BODY" "$PYTHON_BIN" - <<'PY'
import json, os, sys
try:
    data = json.loads(os.environ["BODY"])
except (KeyError, ValueError, TypeError):
    raise SystemExit(1)
tid = data.get("id")
if tid is None:
    raise SystemExit(1)
print(tid)
PY
)" || fail_step "$STEP" "$CODE" "$BODY"
echo "  OK $STEP (task_id=$TASK_ID)"

# ---------------------------------------------------------------------------
# 4) positive: CSP /v1/chat/completions → Router(stream)
# ---------------------------------------------------------------------------
STEP="4_chat_completions_stream"
CHAT_PAYLOAD="$(
  CHAT_PROMPT="$CHAT_PROMPT" "$PYTHON_BIN" - <<'PY'
import json, os
print(json.dumps({
    "model": "anila-router",
    "stream": True,
    "messages": [{"role": "user", "content": os.environ["CHAT_PROMPT"]}],
}, ensure_ascii=False, separators=(",", ":")))
PY
)"
CODE="$(curl_json_post "$BASE/v1/chat/completions" "$CHAT_PAYLOAD" \
  -b "$COOKIE_JAR" \
  -H "X-CSRF-Token: $CSRF_TOKEN" \
  -H "X-ANILA-Conversation-Id: $CONV_ID" \
  -H "X-ANILA-Task-Id: $TASK_ID" \
  --max-time 180)"
BODY="$(cat "$BODY_FILE")"
if [[ "$CODE" != "200" ]]; then
  fail_step "$STEP" "$CODE" "$BODY"
fi
# 斷言 SSE 含 content delta 與終止標記(照 2026-07-24 人工實測契約)。
if ! printf '%s' "$BODY" | grep -q '"content"'; then
  fail_step "$STEP" "$CODE" "missing \"content\" delta in SSE; body=${BODY}"
fi
if ! printf '%s' "$BODY" | grep -Fq 'data: [DONE]'; then
  fail_step "$STEP" "$CODE" "missing data: [DONE] in SSE; body=${BODY}"
fi
echo "  OK $STEP (SSE content + [DONE])"

# ---------------------------------------------------------------------------
# 5) negative: 直打 /router/ 無 X-ANILA-Router-Context → 必須 401
#    (不可跳過;鎖住 R3 formal 契約)
# ---------------------------------------------------------------------------
STEP="5_router_direct_no_context_401"
NEG_PAYLOAD='{"model":"anila-router","stream":false,"messages":[{"role":"user","content":"neg"}]}'
CODE="$(curl_json_post "$BASE/router/v1/chat/completions" "$NEG_PAYLOAD" \
  -b "$COOKIE_JAR" \
  --max-time 30)"
BODY="$(cat "$BODY_FILE")"
if [[ "$CODE" != "401" ]]; then
  fail_step "$STEP" "$CODE" "expected HTTP 401; body=${BODY}"
fi
if ! printf '%s' "$BODY" | grep -Fq 'Router-Context'; then
  fail_step "$STEP" "$CODE" "401 body missing Router-Context; body=${BODY}"
fi
echo "  OK $STEP (401 + Router-Context)"

echo
echo "PASS: router-chain-e2e login→conversation→task→/v1/chat/completions(SSE)→/router 直通 401"
echo "  conversation_id=$CONV_ID task_id=$TASK_ID BASE=$BASE"
