#!/bin/bash
# Phase 1 Step 9+10 E2E sanity — exercises the grant flow against the live
# stack via the same endpoints the admin UI uses. Self-contained: creates
# its own department + clean-up at the end so re-runs are idempotent.
set -e

CSP=http://localhost:8000
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
section() { printf '\n\033[1;36m── %s ──\033[0m\n' "$*"; }

login() {
  curl -s -X POST "$CSP/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$2\"}" \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('access_token','LOGIN_FAILED'))"
}

req() {
  # $1 token, $2 method, $3 path, [$4 body]
  local extra=()
  [ -n "$4" ] && extra=(-H 'Content-Type: application/json' -d "$4")
  curl -s -X "$2" "$CSP$3" -H "Authorization: Bearer $1" "${extra[@]}"
}

ADMIN_TOKEN=$(login admin changeme)
USER_TOKEN=$(login smoke-user changeme)
DEV_TOKEN=$(login 1140921 changeme)
[ "$ADMIN_TOKEN" = "LOGIN_FAILED" ] && { red "admin 登入失敗"; exit 1; }

NOTEBOOK_ID=$(req "$ADMIN_TOKEN" GET "/api/platform-links?include_inactive=true" | python3 -c "import json,sys;print([l['id'] for l in json.load(sys.stdin) if l['name']=='NotebookLM'][0])")
section "Setup: NotebookLM platform_link id=$NOTEBOOK_ID"

# Step 9 — department-scoped grant flow
section "Step 9 — admin grants department access → users in that dept see NotebookLM"

# Create department if not exists
DEPT_NAME="工程部-e2e-$$"
DEPT_ID=$(req "$ADMIN_TOKEN" POST "/api/departments" "{\"name\":\"$DEPT_NAME\",\"description\":\"E2E test dept\"}" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or d.get('detail','FAIL'))")
echo "Created department '$DEPT_NAME' id=$DEPT_ID"

# Move smoke-user into that department
SMOKE_ID=$(req "$ADMIN_TOKEN" GET "/api/users" | python3 -c "import json,sys;print([u['id'] for u in json.load(sys.stdin) if u['username']=='smoke-user'][0])")
req "$ADMIN_TOKEN" PUT "/api/users/$SMOKE_ID" "{\"department_id\":$DEPT_ID}" >/dev/null
echo "Moved smoke-user (id=$SMOKE_ID) into department $DEPT_ID"

# Refresh user token (department change may not invalidate JWT but be safe)
USER_TOKEN=$(login smoke-user changeme)

# Verify smoke-user CANNOT see NotebookLM yet (grant not issued)
BEFORE=$(req "$USER_TOKEN" GET "/api/platform-links" | python3 -c "import json,sys;print('YES' if any(l['id']==$NOTEBOOK_ID for l in json.load(sys.stdin)) else 'NO')")
echo "  smoke-user sees NotebookLM BEFORE grant: $BEFORE  (expected: NO)"
[ "$BEFORE" = "NO" ] && green "    ✓ default-deny" || red "    ✗ unexpected pass-through"

# Admin issues department grant
GRANT_RESP=$(req "$ADMIN_TOKEN" POST "/api/service-access-grants" "{\"platform_link_id\":$NOTEBOOK_ID,\"department_id\":$DEPT_ID}")
GRANT_ID=$(echo "$GRANT_RESP" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or d.get('detail','FAIL'))")
echo "Admin granted dept $DEPT_ID → NotebookLM (grant id=$GRANT_ID)"

# Verify smoke-user CAN see NotebookLM now
AFTER=$(req "$USER_TOKEN" GET "/api/platform-links" | python3 -c "import json,sys;print('YES' if any(l['id']==$NOTEBOOK_ID for l in json.load(sys.stdin)) else 'NO')")
echo "  smoke-user sees NotebookLM AFTER grant:  $AFTER  (expected: YES)"
[ "$AFTER" = "YES" ] && green "    ✓ dept grant unlocks visibility" || red "    ✗ grant not propagated"

# Revoke and verify it disappears again
req "$ADMIN_TOKEN" DELETE "/api/service-access-grants/$GRANT_ID" >/dev/null
AFTER_REVOKE=$(req "$USER_TOKEN" GET "/api/platform-links" | python3 -c "import json,sys;print('YES' if any(l['id']==$NOTEBOOK_ID for l in json.load(sys.stdin)) else 'NO')")
echo "  smoke-user sees NotebookLM AFTER revoke: $AFTER_REVOKE  (expected: NO)"
[ "$AFTER_REVOKE" = "NO" ] && green "    ✓ revoke removes visibility" || red "    ✗ revoke leaked"

# Step 10 — codeserver + GitLab + n8n smoke through nginx
section "Step 10 — same-origin paths reachable via nginx (port 443)"

for path in "/codeserver/" "/gitlab/" "/n8n/"; do
  HTTP=$(curl -sk -L -o /dev/null -w "%{http_code}" "https://localhost$path")
  TITLE=$(curl -sk -L "https://localhost$path" | grep -oE "<title>[^<]*</title>" | head -1)
  echo "  $path → HTTP $HTTP $TITLE"
done

# WebSocket upgrade probe — we don't fully open a WS, just verify nginx
# returns 101 Switching Protocols. code-server's WS endpoint is at root.
WS_HTTP=$(curl -sk -i -o /dev/null -w "%{http_code}" \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  "https://localhost/codeserver/")
echo "  /codeserver/ WS upgrade probe → HTTP $WS_HTTP  (expected 101 or 400 if no auth)"

# Cleanup
section "Cleanup"
req "$ADMIN_TOKEN" PUT "/api/users/$SMOKE_ID" '{"department_id":null}' >/dev/null
echo "  smoke-user moved out of test department"
docker exec anila-platform-csp-db-1 psql -U csp -d csp -c "DELETE FROM service_access_grants WHERE department_id=$DEPT_ID;" 2>&1 | tail -1
docker exec anila-platform-csp-db-1 psql -U csp -d csp -c "DELETE FROM departments WHERE id=$DEPT_ID;" 2>&1 | tail -1
green "Cleanup done"

section "Final state"
docker exec anila-platform-csp-db-1 psql -U csp -d csp -c "SELECT count(*) AS active_grants FROM service_access_grants WHERE revoked_at IS NULL;"
docker exec anila-platform-csp-db-1 psql -U csp -d csp -c "SELECT count(*) AS departments FROM departments;"
