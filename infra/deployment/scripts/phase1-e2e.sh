#!/bin/bash
# Phase 1 Step 9+10 E2E sanity — exercises the grant flow against the live
# stack via the same endpoints the admin UI uses. Self-contained: creates
# its own department + clean-up at the end so re-runs are idempotent.
set -e
E2E_FAILED=0

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

NOTEBOOK_ID=$(req "$ADMIN_TOKEN" GET "/api/platform-links?include_inactive=true" | python3 -c "import json,sys;print([l['id'] for l in json.load(sys.stdin) if l['name']=='ANILA LM'][0])")
section "Setup: ANILA LM platform_link id=$NOTEBOOK_ID"

# Step 9 — department-scoped grant flow
section "Step 9 — admin grants department access → users in that dept see ANILA LM"

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

# Verify smoke-user CANNOT see ANILA LM yet (grant not issued)
BEFORE=$(req "$USER_TOKEN" GET "/api/platform-links" | python3 -c "import json,sys;print('YES' if any(l['id']==$NOTEBOOK_ID for l in json.load(sys.stdin)) else 'NO')")
echo "  smoke-user sees ANILA LM BEFORE grant: $BEFORE  (expected: NO)"
[ "$BEFORE" = "NO" ] && green "    ✓ default-deny" || red "    ✗ unexpected pass-through"

# Admin issues department grant
GRANT_RESP=$(req "$ADMIN_TOKEN" POST "/api/service-access-grants" "{\"platform_link_id\":$NOTEBOOK_ID,\"department_id\":$DEPT_ID}")
GRANT_ID=$(echo "$GRANT_RESP" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id') or d.get('detail','FAIL'))")
echo "Admin granted dept $DEPT_ID → ANILA LM (grant id=$GRANT_ID)"

# Verify smoke-user CAN see ANILA LM now
AFTER=$(req "$USER_TOKEN" GET "/api/platform-links" | python3 -c "import json,sys;print('YES' if any(l['id']==$NOTEBOOK_ID for l in json.load(sys.stdin)) else 'NO')")
echo "  smoke-user sees ANILA LM AFTER grant:  $AFTER  (expected: YES)"
[ "$AFTER" = "YES" ] && green "    ✓ dept grant unlocks visibility" || red "    ✗ grant not propagated"

# Revoke and verify it disappears again
req "$ADMIN_TOKEN" DELETE "/api/service-access-grants/$GRANT_ID" >/dev/null
AFTER_REVOKE=$(req "$USER_TOKEN" GET "/api/platform-links" | python3 -c "import json,sys;print('YES' if any(l['id']==$NOTEBOOK_ID for l in json.load(sys.stdin)) else 'NO')")
echo "  smoke-user sees ANILA LM AFTER revoke: $AFTER_REVOKE  (expected: NO)"
[ "$AFTER_REVOKE" = "NO" ] && green "    ✓ revoke removes visibility" || red "    ✗ revoke leaked"

# Step 10 — retained developer tools use distinct browser origins.  This keeps
# their active content outside the CSP origin's ambient cookie authority; each
# upstream then applies its own native login.  --resolve exercises real nginx
# virtual-host selection without depending on workstation DNS during rollout.
section "Step 10 — isolated developer-tool origins (port 443)"

PLATFORM_HTTPS="${ANILA_E2E_PLATFORM_URL:-https://localhost}"
N8N_E2E_HOST="${N8N_HOST:-n8n.ai.ncsist.org.tw}"
GITLAB_E2E_HOST="${GITLAB_HOST:-gitlab.ai.ncsist.org.tw}"
CODESERVER_E2E_HOST="${CODESERVER_HOST:-code.ai.ncsist.org.tw}"

for path in "/n8n" "/n8n/e2e" "/gitlab" "/gitlab/e2e" "/codeserver" "/codeserver/e2e"; do
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" "$PLATFORM_HTTPS$path")
  if [ "$HTTP" = 404 ]; then
    green "  ✓ platform $path → 404 (no tool active content on CSP origin)"
  else
    red "  ✗ platform $path → $HTTP (expected 404)"
    E2E_FAILED=1
  fi
done

for spec in "n8n:$N8N_E2E_HOST" "gitlab:$GITLAB_E2E_HOST"; do
  name="${spec%%:*}"
  host="${spec#*:}"
  HTTP=$(curl -sk --resolve "$host:443:127.0.0.1" -o /dev/null -w "%{http_code}" \
    "https://$host/")
  case "$HTTP" in
    2??|3??) green "  ✓ $name isolated origin https://$host/ → $HTTP (native UI reachable)" ;;
    *) red "  ✗ $name isolated origin https://$host/ → $HTTP"; E2E_FAILED=1 ;;
  esac
done

for spec in "n8n:$N8N_E2E_HOST:/n8n" \
            "gitlab:$GITLAB_E2E_HOST:/gitlab" \
            "code-server:$CODESERVER_E2E_HOST:/codeserver"; do
  name="${spec%%:*}"
  remainder="${spec#*:}"
  host="${remainder%%:*}"
  path="${remainder#*:}"
  HTTP=$(curl -sk --resolve "$host:443:127.0.0.1" -o /dev/null -w "%{http_code}" \
    "https://$host$path")
  if [ "$HTTP" = 404 ]; then
    green "  ✓ $name legacy subpath $path → 404 (root is the sole canonical base)"
  else
    red "  ✗ $name legacy subpath $path → $HTTP (expected 404)"
    E2E_FAILED=1
  fi
done

for path in "/webhook" "/WEBHOOK/e2e" "/webhook-test" "/webhook-waiting/e2e" \
            "/form" "/FORM/e2e" "/form-test" "/form-waiting/e2e" \
            "/mcp/e2e" "/MCP-TEST/e2e" "/mcp-server/http" \
            "/mcp-oauth/register" "/OAUTH/token" \
            "/.well-known/oauth-authorization-server" \
            "/.well-known/oauth-protected-resource/mcp/e2e"; do
  HTTP=$(curl -sk --resolve "$N8N_E2E_HOST:443:127.0.0.1" \
    -o /dev/null -w "%{http_code}" "https://$N8N_E2E_HOST$path")
  if [ "$HTTP" = 404 ]; then
    green "  ✓ n8n $path → 404 (unauthenticated machine ingress remains closed)"
  else
    red "  ✗ n8n $path → $HTTP (expected 404)"
    E2E_FAILED=1
  fi
done

# Cookies are scoped by hostname, not port. Tool hosts must never reach CSP on
# the separately published ANILA UI port or CSP could mint cookies for them.
for host in "$N8N_E2E_HOST" "$GITLAB_E2E_HOST" "$CODESERVER_E2E_HOST"; do
  HTTP=$(curl -sk --max-time 5 --resolve "$host:4443:127.0.0.1" \
    -o /dev/null -w "%{http_code}" "https://$host:4443/" 2>/dev/null || true)
  if [ "$HTTP" = 000 ] || [ "$HTTP" = 444 ]; then
    green "  ✓ tool host $host rejected on CSP-bearing :4443"
  else
    red "  ✗ tool host $host reached :4443 → $HTTP (expected rejection)"
    E2E_FAILED=1
  fi
done

if [ "${ANILA_E2E_CODESERVER:-0}" = 1 ]; then
  CS_HTTP=$(curl -sk --resolve "$CODESERVER_E2E_HOST:443:127.0.0.1" \
    -o /dev/null -w "%{http_code}" "https://$CODESERVER_E2E_HOST/")
  case "$CS_HTTP" in
    3??|401) green "  ✓ code-server isolated origin → $CS_HTTP (native password challenge)" ;;
    *) red "  ✗ code-server isolated origin → $CS_HTTP (expected redirect/401)"; E2E_FAILED=1 ;;
  esac
else
  echo "  code-server isolated origin skipped (opt-in profile; set ANILA_E2E_CODESERVER=1 to test)"
fi

# Cleanup
section "Cleanup"
req "$ADMIN_TOKEN" PUT "/api/users/$SMOKE_ID" '{"department_id":null}' >/dev/null
echo "  smoke-user moved out of test department"
# L2: 用 psql -v 變數注入而不是 shell 字串內插，避免 $DEPT_ID 含意外字元
# 時破壞 SQL（雖然當前流程確保是整數，但這樣更安全也容易 review）。
docker exec anila-platform-csp-db-1 psql -U csp -d csp \
  -v dept_id="$DEPT_ID" \
  -c "DELETE FROM service_access_grants WHERE department_id=:dept_id;" 2>&1 | tail -1
docker exec anila-platform-csp-db-1 psql -U csp -d csp \
  -v dept_id="$DEPT_ID" \
  -c "DELETE FROM departments WHERE id=:dept_id;" 2>&1 | tail -1
green "Cleanup done"

section "Final state"
docker exec anila-platform-csp-db-1 psql -U csp -d csp -c "SELECT count(*) AS active_grants FROM service_access_grants WHERE revoked_at IS NULL;"
docker exec anila-platform-csp-db-1 psql -U csp -d csp -c "SELECT count(*) AS departments FROM departments;"

if [ "$E2E_FAILED" -ne 0 ]; then
  red "E2E completed with policy-smoke failures"
  exit 1
fi
