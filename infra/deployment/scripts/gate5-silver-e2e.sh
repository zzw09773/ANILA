#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# Disposable Gate 5 Silver E2E.  It deliberately uses only the development
# Compose profile: the formal platform profile needs a signed registry overlay
# and operator-provisioned credentials, while this harness owns its ephemeral
# rows and removes them with its isolated project/volumes.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"
# shellcheck disable=SC1091
source "$ROOT_DIR/infra/deployment/scripts/ensure-models-network.sh"
PROJECT="gate5-silver-e2e"
COMPOSE_FILE="compose.dev.yaml"
PROFILE="gate5-silver"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN=python
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "BLOCKED: Docker CLI is required for Gate 5 Silver E2E" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "BLOCKED: Docker daemon is unavailable" >&2
  exit 2
fi

COMPOSE=(docker compose --project-name "$PROJECT" --file "$COMPOSE_FILE" --profile "$PROFILE")
# Keep path conversion disabled for the Compose wrapper.  The seed helper is
# streamed over stdin below so a mode-restricted checkout cannot turn its
# read-only bind mount into a root-only file inside the csp container.
compose() { MSYS_NO_PATHCONV=1 "${COMPOSE[@]}" "$@"; }

RUN_ID="gate5-$(date -u +%Y%m%dT%H%M%SZ)-$($PYTHON_BIN -c 'import secrets; print(secrets.token_hex(6))')"
AGENT_ID="anila-agent-silver-$RUN_ID"
MODEL_TEMP_NAME="gate5-model-$RUN_ID"
AGENT_TOKEN="csk-$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(32))')"
ROUTER_TOKEN="csk-$($PYTHON_BIN -c 'import secrets; print(secrets.token_urlsafe(32))')"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gate5-silver.XXXXXX")"
export ANILA_DEV_TLS_CERTS_DIR="$TMP_DIR/tls"
mkdir -p "$ANILA_DEV_TLS_CERTS_DIR"

# Dev compose has one content-identity guard even when only CSP is started.
export EMBEDDING_MODEL_FINGERPRINT_DEV="${EMBEDDING_MODEL_FINGERPRINT_DEV:-sha256:0000000000000000000000000000000000000000000000000000000000000000}"
export ANILA_DEPLOYMENT_PROFILE_DEV=development
export ANILA_AGENT_SERVICE_TOKEN_DEV="$AGENT_TOKEN"
export ANILA_AGENT_ID_DEV="$AGENT_ID"
export ANILA_AGENT_NAME_DEV="$AGENT_ID"
# The Router's named service client is seeded below.  Wire every formal
# Router transport to that one ephemeral token; the Agent keeps its own csk-.
# Keep CSP_SERVICE_TOKEN on the dev-only fallback (dev-service-token): setting
# it to the Router token would make CSP startup treat the same token as a
# legacy env credential before the unique named ServiceClient row is seeded.
export ANILA_CSP_REGISTRY_SERVICE_TOKEN="$ROUTER_TOKEN"
export ANILA_CSP_AGENT_SERVICE_TOKEN="$ROUTER_TOKEN"
export ANILA_CSP_INFERENCE_SERVICE_TOKEN="$ROUTER_TOKEN"
export ANILA_E2E_REQUIRE_TOOL_APPROVAL=1
export ANILA_E2E_HARNESS=gate5-silver

cleanup() {
  local rc=$?
  if [[ "${GATE5_E2E_KEEP:-0}" != "1" ]]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$TMP_DIR"
  else
    echo "GATE5_E2E_KEEP=1: preserved project $PROJECT and $TMP_DIR" >&2
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

ensure_models_network

# Fail before creating containers if interpolation, profile isolation or
# read-only state wiring is invalid.  Do not print the resolved environment.
compose config >/dev/null
compose up -d --build csp-db redis csp router gate5-e2e-model >/dev/null

wait_ready() {
  local service="$1" url="$2" attempts="${3:-60}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if compose exec -T "$service" "$PYTHON_BIN" -c \
      "import urllib.request; urllib.request.urlopen('$url', timeout=2).read()" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "BLOCKED: $service did not become ready ($url)" >&2
  return 1
}

# A container-level /ready only proves that the CSP process can serve HTTP.
# Agent health is written by CSP's background checker and is intentionally
# part of the caller-scoped registry snapshot hash.  After a coordinated
# restart the first probe can therefore publish a fail-closed ``unhealthy``
# snapshot before the Agent is reachable again.  Poll the CSP-owned registry
# authority instead of sleeping a guessed number of seconds; resume remains
# blocked until the exact pre-pause snapshot and ready Agent entry converge.
wait_registry_converged() {
  local caller_user_id="$1" expected_agent_id="$2" expected_snapshot_hash="$3"
  local attempts="${4:-90}" i state
  for ((i = 1; i <= attempts; i++)); do
    if state="$(
      compose exec -T \
        -e E2E_REGISTRY_SERVICE_TOKEN="$ROUTER_TOKEN" \
        -e E2E_REGISTRY_CALLER_USER_ID="$caller_user_id" \
        -e E2E_REGISTRY_AGENT_ID="$expected_agent_id" \
        -e E2E_REGISTRY_SNAPSHOT_HASH="$expected_snapshot_hash" \
        csp "$PYTHON_BIN" - <<'PY'
import json
import os
import urllib.error
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8000/internal/v1/agents/registry",
    headers={
        "X-CSP-Service-Token": os.environ["E2E_REGISTRY_SERVICE_TOKEN"],
        "X-ANILA-Caller-User-Id": os.environ["E2E_REGISTRY_CALLER_USER_ID"],
    },
)
try:
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
except (
    urllib.error.HTTPError,
    urllib.error.URLError,
    TimeoutError,
    TypeError,
    ValueError,
    OSError,
):
    print("not-ready")
else:
    if not isinstance(payload, dict):
        print("not-ready")
    else:
        raw_entries = payload.get("agents")
        if not isinstance(raw_entries, list):
            print("not-ready")
        else:
            entries = [
                item
                for item in raw_entries
                if isinstance(item, dict)
                and item.get("agent_id") == os.environ["E2E_REGISTRY_AGENT_ID"]
            ]
            converged = (
                payload.get("snapshot_id") == os.environ["E2E_REGISTRY_SNAPSHOT_HASH"]
                and payload.get("snapshot_revision")
                == os.environ["E2E_REGISTRY_SNAPSHOT_HASH"]
                and payload.get("snapshot_hash")
                == os.environ["E2E_REGISTRY_SNAPSHOT_HASH"]
                and payload.get("registry_snapshot_id")
                == os.environ["E2E_REGISTRY_SNAPSHOT_HASH"]
                and len(entries) == 1
                and entries[0].get("health_status") == "healthy"
                and entries[0].get("health_ready") is True
                and entries[0].get("ready_for_dispatch") is True
            )
            print("ready" if converged else "not-ready")
PY
    )"; then
      :
    else
      # A transient exec/container failure is not readiness evidence.  Keep
      # polling rather than turning an unavailable probe into a resume.
      state="not-ready"
    fi
    if [[ "$state" == "ready" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "BLOCKED: CSP registry/Agent readiness did not converge to the persisted snapshot" >&2
  return 1
}

wait_ready csp http://127.0.0.1:8000/ready 90
wait_ready router http://127.0.0.1:9000/health 60
wait_ready gate5-e2e-model http://127.0.0.1:8105/ready 45

SEED_HELPER="$ROOT_DIR/infra/deployment/scripts/gate5-silver-seed.py"

# Model row is seeded first so the Agent manifest can bind to its numeric CSP
# model id.  The seed helper never creates an ExecutionGrant.
MODEL_ID="$(compose exec -T \
  -e PYTHONPATH=/app \
  -e E2E_MODEL_TEMP_NAME="$MODEL_TEMP_NAME" \
  csp "$PYTHON_BIN" - model < "$SEED_HELPER" | tail -n 1 | tr -d '\r')"
if [[ ! "$MODEL_ID" =~ ^[1-9][0-9]*$ ]]; then
  echo "BLOCKED: CSP model seed did not return a numeric id" >&2
  exit 1
fi
export ANILA_AGENT_MODEL_ID_DEV="$MODEL_ID"

compose up -d --build anila-agent >/dev/null
wait_ready anila-agent http://127.0.0.1:8200/ready 90

CONTEXT_JSON="$(compose exec -T \
  -e PYTHONPATH=/app \
  -e E2E_AGENT_NAME="$AGENT_ID" \
  -e E2E_AGENT_TOKEN="$AGENT_TOKEN" \
  -e E2E_ROUTER_TOKEN="$ROUTER_TOKEN" \
  -e E2E_MODEL_ID="$MODEL_ID" \
  -e E2E_INCLUDE_ACCESS_TOKEN=1 \
  csp "$PYTHON_BIN" - authority < "$SEED_HELPER" | tail -n 1 | tr -d '\r')"
export CONTEXT_JSON
if ! "$PYTHON_BIN" -c 'import json,os; json.loads(os.environ["CONTEXT_JSON"])' >/dev/null; then
  echo "BLOCKED: CSP authority seed did not return context JSON" >&2
  exit 1
fi

context_value() {
  local key="$1"
  CONTEXT_JSON="$CONTEXT_JSON" CONTEXT_KEY="$key" "$PYTHON_BIN" -c \
    'import json,os; value=json.loads(os.environ["CONTEXT_JSON"])[os.environ["CONTEXT_KEY"]]; print(json.dumps(value,ensure_ascii=True,separators=(",",":")) if isinstance(value,(dict,list)) else value)'
}

USER_ID="$(context_value user_id)"
TASK_ID="$(context_value task_id)"
RUN_DB_ID="$(context_value run_id)"
SOURCE_ID="$(context_value source_snapshot_id)"
TRACE_ID="$(context_value trace_id)"
INVOCATION_ID="$(context_value invocation_id)"
SESSION_ID="$(context_value session_id)"
REGISTRY_ID="$(context_value registry_snapshot_id)"
REGISTRY_REVISION="$(context_value registry_snapshot_revision)"
REGISTRY_HASH="$(context_value registry_snapshot_hash)"
MANIFEST_REVISION="$(context_value manifest_revision)"
MANIFEST_HASH="$(context_value manifest_sha256)"
ROUTE_ID="$(context_value route_decision_id)"
POLICY_ID="$(context_value policy_decision_id)"
AUTH_ASSURANCE="$(context_value auth_assurance)"
MODEL_BINDING="$(context_value model_binding)"
PUBLIC_ACCESS_TOKEN="$(context_value public_access_token)"
if [[ -z "$PUBLIC_ACCESS_TOKEN" ]]; then
  echo "BLOCKED: authority seed did not return a public access token" >&2
  exit 1
fi

MINT_PAYLOAD="$(
  CONTEXT_JSON="$CONTEXT_JSON" MODEL_ID="$MODEL_ID" "$PYTHON_BIN" - <<'PY'
import json, os
ctx = json.loads(os.environ["CONTEXT_JSON"])
model_id = int(os.environ["MODEL_ID"])
payload = {
    "task_id": ctx["task_id"],
    "run_id": ctx["run_id"],
    "source_snapshot_id": ctx["source_snapshot_id"],
    "trace_id": ctx["trace_id"],
    "invocation_id": ctx["invocation_id"],
    "session_id": ctx["session_id"],
    "registry_snapshot_id": ctx["registry_snapshot_id"],
    "registry_snapshot_revision": ctx["registry_snapshot_revision"],
    "registry_snapshot_hash": ctx["registry_snapshot_hash"],
    "target_agent_id": ctx["agent_id"],
    "manifest_revision": ctx["manifest_revision"],
    "manifest_sha256": ctx["manifest_sha256"],
    "classification": "無機密",
    "auth_assurance": ctx["auth_assurance"],
    "model_binding": ctx["model_binding"],
    "allowed_capabilities": ["tools"],
    "allowed_scopes": ["agent:invoke"],
    "route_decision": {
        "schema_version": "route-decision/v1",
        "decision_id": ctx["route_decision_id"],
        "route_type": "single_agent",
        "registry_snapshot_id": ctx["registry_snapshot_id"],
        "required_capabilities": ["tools"],
        "candidate_agent_ids": [ctx["agent_id"]],
        "selected_agent_id": ctx["agent_id"],
        "reason_codes": ["gate5_e2e"],
        "confidence": 1.0,
        "rewritten_query": "Gate 5 Silver durable resume",
        "constraints": {"max_steps": 10, "timeout_ms": 60000},
    },
    "policy_result": {
        "schema_version": "policy-gate/v1",
        "allowed": True,
        "decision_id": ctx["policy_decision_id"],
        "route_decision_id": ctx["route_decision_id"],
        "registry_snapshot_id": ctx["registry_snapshot_id"],
        "target_agent_id": ctx["agent_id"],
        "effective_classification": "無機密",
        "required_scopes": ["agent:invoke"],
        "obligations": ["FULL_TRACE"],
        "approval_required": False,
        "reason_codes": ["gate5_e2e_allow"],
    },
    "ttl_seconds": 60,
}
# MINT_PAYLOAD crosses the Git Bash/Windows host boundary through an
# environment variable and docker compose exec. Keep machine JSON ASCII-only
# so the classification enum remains valid after transport.
print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
PY
)"

csp_http_post() {
  local path="$1" payload="$2" extra_headers="${3-}" access_token="${4-}"
  if [[ -z "$extra_headers" ]]; then
    extra_headers='{}'
  fi
  E2E_PATH="$path" E2E_PAYLOAD="$payload" E2E_HEADERS="$extra_headers" \
    E2E_ROUTER_TOKEN="$ROUTER_TOKEN" E2E_ACCESS_TOKEN="$access_token" compose exec -T \
    -e E2E_PATH -e E2E_PAYLOAD -e E2E_HEADERS -e E2E_ROUTER_TOKEN -e E2E_ACCESS_TOKEN \
    csp "$PYTHON_BIN" - <<'PY'
import json, os, sys, urllib.error, urllib.request
path = os.environ["E2E_PATH"]
headers = {
    "Content-Type": "application/json",
    "X-CSP-Service-Token": os.environ["E2E_ROUTER_TOKEN"],
}
access_token = os.environ.get("E2E_ACCESS_TOKEN")
if access_token:
    headers["Authorization"] = "Bearer " + access_token
headers.update(json.loads(os.environ.get("E2E_HEADERS", "{}")))
request = urllib.request.Request(
    "http://127.0.0.1:8000" + path,
    data=os.environ["E2E_PAYLOAD"].encode("utf-8"),
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=90) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    if exc.code in {400, 403, 409, 422, 500, 502, 503}:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = {}
        detail = parsed.get("detail") if isinstance(parsed, dict) else None

        def safe_text(value):
            if not isinstance(value, str):
                return None
            value = "".join(char for char in value if char.isprintable()).strip()
            return value[:256] or None

        if exc.code == 422 and isinstance(detail, list):
            safe_errors = []
            for item in detail:
                if not isinstance(item, dict):
                    continue
                safe_item = {}
                for field in ("loc", "type", "msg"):
                    value = item.get(field)
                    if field == "loc" and isinstance(value, list):
                        safe_item[field] = [
                            part if isinstance(part, int) and not isinstance(part, bool)
                            else safe_text(part)
                            for part in value[:16]
                        ]
                    else:
                        safe_item[field] = safe_text(value)
                safe_errors.append(safe_item)
            safe_detail = json.dumps(
                {"detail": safe_errors}, ensure_ascii=True, separators=(",", ":")
            )
            print(f"CSP HTTP 422 at {path}: {safe_detail}", file=sys.stderr)
        else:
            safe_detail = safe_text(detail) or "detail unavailable"
            print(f"CSP HTTP {exc.code} at {path}: detail={safe_detail}", file=sys.stderr)
    else:
        print(f"CSP HTTP {exc.code} at {path}", file=sys.stderr)
    raise SystemExit(1)
PY
}

csp_http_get() {
  local path="$1" access_token="$2" extra_headers="${3-}"
  if [[ -z "$extra_headers" ]]; then
    extra_headers='{}'
  fi
  E2E_PATH="$path" E2E_HEADERS="$extra_headers" E2E_ACCESS_TOKEN="$access_token" \
    compose exec -T -e E2E_PATH -e E2E_HEADERS -e E2E_ACCESS_TOKEN \
    csp "$PYTHON_BIN" - <<'PY'
import json, os, sys, urllib.error, urllib.request
path = os.environ["E2E_PATH"]
headers = {"Authorization": "Bearer " + os.environ["E2E_ACCESS_TOKEN"]}
headers.update(json.loads(os.environ.get("E2E_HEADERS", "{}")))
request = urllib.request.Request("http://127.0.0.1:8000" + path, headers=headers, method="GET")
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type.lower():
            print(f"CSP GET at {path} did not return JSON", file=sys.stderr)
            raise SystemExit(1)
        body = response.read().decode("utf-8")
except urllib.error.HTTPError as exc:
    print(f"CSP GET HTTP {exc.code} at {path}", file=sys.stderr)
    raise SystemExit(1)
except (UnicodeDecodeError, ValueError):
    print(f"CSP GET at {path} returned invalid JSON bytes", file=sys.stderr)
    raise SystemExit(1)
try:
    parsed = json.loads(body)
except (UnicodeDecodeError, ValueError):
    print(f"CSP GET at {path} returned invalid JSON", file=sys.stderr)
    raise SystemExit(1)
sys.stdout.write(json.dumps(parsed, ensure_ascii=True, separators=(",", ":")))
PY
}

router_http_post() {
  # This public Router seam resolves the durable CSP ``resume-by-session``
  # authority internally; the harness never forwards the original grant JWT.
  local path="$1" payload="$2" extra_headers="${3-}"
  if [[ -z "$extra_headers" ]]; then
    extra_headers='{}'
  fi
  ROUTER_PATH="$path" ROUTER_PAYLOAD="$payload" ROUTER_HEADERS="$extra_headers" \
    ROUTER_ACCESS_TOKEN="$PUBLIC_ACCESS_TOKEN" compose exec -T \
    -e ROUTER_PATH -e ROUTER_PAYLOAD -e ROUTER_HEADERS -e ROUTER_ACCESS_TOKEN \
    csp "$PYTHON_BIN" - <<'PY'
import json, os, sys, urllib.error, urllib.request
path = os.environ["ROUTER_PATH"]
headers = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + os.environ["ROUTER_ACCESS_TOKEN"],
}
headers.update(json.loads(os.environ.get("ROUTER_HEADERS", "{}")))
request = urllib.request.Request(
    "http://router:9000" + path,
    data=os.environ["ROUTER_PAYLOAD"].encode("utf-8"),
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    if exc.code in {400, 403, 409, 422, 500, 502, 503}:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = {}
        detail = parsed.get("detail") if isinstance(parsed, dict) else None

        def safe_text(value):
            if not isinstance(value, str):
                return None
            value = "".join(char for char in value if char.isprintable()).strip()
            return value[:256] or None

        if exc.code == 422 and isinstance(detail, list):
            safe_errors = []
            for item in detail:
                if not isinstance(item, dict):
                    continue
                safe_item = {}
                for field in ("loc", "type", "msg"):
                    value = item.get(field)
                    if field == "loc" and isinstance(value, list):
                        safe_item[field] = [
                            part if isinstance(part, int) and not isinstance(part, bool)
                            else safe_text(part)
                            for part in value[:16]
                        ]
                    else:
                        safe_item[field] = safe_text(value)
                safe_errors.append(safe_item)
            safe_detail = json.dumps(
                {"detail": safe_errors}, ensure_ascii=True, separators=(",", ":")
            )
            print(f"Router HTTP 422 at {path}: {safe_detail}", file=sys.stderr)
        else:
            safe_detail = safe_text(detail) or "detail unavailable"
            print(f"Router HTTP {exc.code} at {path}: detail={safe_detail}", file=sys.stderr)
    else:
        print(f"Router HTTP {exc.code} at {path}", file=sys.stderr)
    raise SystemExit(1)
PY
}

MINT_RESPONSE="$(csp_http_post /internal/v1/execution-grants/mint "$MINT_PAYLOAD" "$(
  USER_ID="$USER_ID" "$PYTHON_BIN" -c 'import json,os; print(json.dumps({"X-ANILA-Caller-User-Id":os.environ["USER_ID"]},separators=(",",":")))'
)")"
GRANT_TOKEN="$(MINT_RESPONSE="$MINT_RESPONSE" "$PYTHON_BIN" -c 'import json,os; print(json.loads(os.environ["MINT_RESPONSE"])["token"])')"
GRANT_ID="$(MINT_RESPONSE="$MINT_RESPONSE" "$PYTHON_BIN" -c 'import json,os; print(json.loads(os.environ["MINT_RESPONSE"])["grant"]["grant_id"])')"
unset MINT_RESPONSE MINT_PAYLOAD

DISPATCH_HEADERS="$(
  USER_ID="$USER_ID" TASK_ID="$TASK_ID" RUN_DB_ID="$RUN_DB_ID" SOURCE_ID="$SOURCE_ID" \
  TRACE_ID="$TRACE_ID" INVOCATION_ID="$INVOCATION_ID" SESSION_ID="$SESSION_ID" \
  AGENT_ID="$AGENT_ID" REGISTRY_ID="$REGISTRY_ID" REGISTRY_REVISION="$REGISTRY_REVISION" \
  REGISTRY_HASH="$REGISTRY_HASH" MANIFEST_REVISION="$MANIFEST_REVISION" MANIFEST_HASH="$MANIFEST_HASH" \
  GRANT_ID="$GRANT_ID" ROUTE_ID="$ROUTE_ID" POLICY_ID="$POLICY_ID" GRANT_TOKEN="$GRANT_TOKEN" \
  "$PYTHON_BIN" - <<'PY'
import json, os
values = {
    "X-ANILA-Caller-User-Id": os.environ["USER_ID"],
    "X-ANILA-Owner-Id": os.environ["USER_ID"],
    "X-ANILA-Task-Id": os.environ["TASK_ID"],
    "X-ANILA-Run-Id": os.environ["RUN_DB_ID"],
    "X-ANILA-Source-Snapshot-Id": os.environ["SOURCE_ID"],
    "X-ANILA-Trace-Id": os.environ["TRACE_ID"],
    "X-ANILA-Invocation-Id": os.environ["INVOCATION_ID"],
    "X-ANILA-Session-Id": os.environ["SESSION_ID"],
    "X-ANILA-Agent-Id": os.environ["AGENT_ID"],
    "X-ANILA-Registry-Snapshot-Id": os.environ["REGISTRY_ID"],
    "X-ANILA-Registry-Snapshot-Revision": os.environ["REGISTRY_REVISION"],
    "X-ANILA-Registry-Snapshot-Hash": os.environ["REGISTRY_HASH"],
    "X-ANILA-Agent-Manifest-Revision": os.environ["MANIFEST_REVISION"],
    "X-ANILA-Agent-Manifest-SHA256": os.environ["MANIFEST_HASH"],
    "X-ANILA-Execution-Grant-Id": os.environ["GRANT_ID"],
    "X-ANILA-Execution-Grant": os.environ["GRANT_TOKEN"],
    "X-ANILA-Route-Decision-Id": os.environ["ROUTE_ID"],
    "X-ANILA-Policy-Decision-Id": os.environ["POLICY_ID"],
    "X-ANILA-Classification-Level": "%E7%84%A1%E6%A9%9F%E5%AF%86",
    "X-ANILA-Idempotency-Key": os.environ["INVOCATION_ID"],
}
print(json.dumps(values, separators=(",", ":")))
PY
)"
export DISPATCH_HEADERS GRANT_TOKEN GRANT_ID

DISPATCH_PAYLOAD="$(
  CONTEXT_JSON="$CONTEXT_JSON" AGENT_ID="$AGENT_ID" "$PYTHON_BIN" - <<'PY'
import json, os
ctx = json.loads(os.environ["CONTEXT_JSON"])
binding = {
    "task_id": ctx["task_id"], "run_id": ctx["run_id"],
    "source_snapshot_id": ctx["source_snapshot_id"], "trace_id": ctx["trace_id"],
    "invocation_id": ctx["invocation_id"], "session_id": ctx["session_id"],
    "owner_id": ctx["user_id"], "agent_id": ctx["agent_id"],
    "registry_snapshot_id": ctx["registry_snapshot_id"],
    "registry_snapshot_revision": ctx["registry_snapshot_revision"],
    "registry_snapshot_hash": ctx["registry_snapshot_hash"],
    "manifest_revision": ctx["manifest_revision"], "manifest_sha256": ctx["manifest_sha256"],
    "grant_id": os.environ.get("GRANT_ID", ""),
    "route_decision_id": ctx["route_decision_id"], "policy_decision_id": ctx["policy_decision_id"],
}
print(json.dumps({
    "model": ctx["agent_id"],
    "messages": [{"role": "user", "content": "Gate 5 Silver durable resume"}],
    "stream": False,
    "anila_session_id": ctx["session_id"],
    "anila_binding": binding,
}, ensure_ascii=True, separators=(",", ":")))
PY
)"
DISPATCH_RESPONSE="$(csp_http_post /internal/v1/agents/dispatch "$DISPATCH_PAYLOAD" "$DISPATCH_HEADERS")"
DISPATCH_STATUS="$(DISPATCH_RESPONSE="$DISPATCH_RESPONSE" "$PYTHON_BIN" -c 'import json,os; print(json.loads(os.environ["DISPATCH_RESPONSE"]).get("status",""))')"
if [[ "$DISPATCH_STATUS" != "paused" ]]; then
  echo "BLOCKED: formal dispatch did not return paused" >&2
  exit 1
fi
unset DISPATCH_RESPONSE DISPATCH_PAYLOAD

# Explicit restart proof: kill and recreate CSP, Router and Agent while
# preserving the CSP DB, Router state and Agent ANILA_HOME named volumes. These
# command forms are part of the Gate 5 check:
# docker compose kill anila-agent
# docker compose up -d --force-recreate --no-deps anila-agent
compose kill router csp anila-agent >/dev/null
compose up -d --force-recreate --no-deps csp router anila-agent >/dev/null
wait_ready csp http://127.0.0.1:8000/ready 90
wait_ready router http://127.0.0.1:9000/health 60
wait_ready anila-agent http://127.0.0.1:8200/ready 90
wait_registry_converged "$USER_ID" "$AGENT_ID" "$REGISTRY_HASH" 90

RESUME_PAYLOAD='{"approval_mode":"approve_all"}'
ROUTER_RESUME_HEADERS="$(
  RUN_ID="$RUN_ID" "$PYTHON_BIN" - <<'PY'
import json, os
print(json.dumps({
    "X-ANILA-Idempotency-Key": "gate5-router-resume-" + os.environ["RUN_ID"],
}, separators=(",", ":")))
PY
)"
RESUME_RESPONSE="$(router_http_post "/v1/sessions/$SESSION_ID/answer" "$RESUME_PAYLOAD" "$ROUTER_RESUME_HEADERS")"
RESUME_STATUS="$(RESUME_RESPONSE="$RESUME_RESPONSE" "$PYTHON_BIN" -c 'import json,os; print(json.loads(os.environ["RESUME_RESPONSE"]).get("status",""))')"
if [[ "$RESUME_STATUS" != "completed" ]]; then
  echo "BLOCKED: Router public formal resume did not return completed" >&2
  exit 1
fi

# Exact same idempotency request must be a durable replay, not a second Agent
# execution.  The response is intentionally not printed (it may contain text).
REPLAY_RESPONSE="$(router_http_post "/v1/sessions/$SESSION_ID/answer" "$RESUME_PAYLOAD" "$ROUTER_RESUME_HEADERS")"
REPLAY_STATUS="$(REPLAY_RESPONSE="$REPLAY_RESPONSE" "$PYTHON_BIN" -c 'import json,os; print(json.loads(os.environ["REPLAY_RESPONSE"]).get("status",""))')"
if [[ "$REPLAY_STATUS" != "completed" ]]; then
  echo "BLOCKED: resume replay was not completed" >&2
  exit 1
fi

# Read the CSP-owned event/authority/attempt ledger from inside the CSP
# (tables: session_events, resume_attempts, resume_authorities).
# container.  No grant or service credential is selected from this query.
LEDGER_JSON="$(
  E2E_RUN_ID="$RUN_DB_ID" compose exec -T -e PYTHONPATH=/app -e E2E_RUN_ID csp "$PYTHON_BIN" - <<'PY'
import json, os
from app.database import SessionLocal
from app.models import ResumeAttempt, ResumeAuthority, SessionEvent, SessionEventRun

run_id = os.environ["E2E_RUN_ID"]
db = SessionLocal()
try:
    ledger = db.query(SessionEventRun).filter(SessionEventRun.run_id == run_id).one()
    events = db.query(SessionEvent).filter(SessionEvent.run_id == run_id).order_by(SessionEvent.cursor).all()
    authority = db.query(ResumeAuthority).filter(ResumeAuthority.run_id == int(run_id)).one()
    attempts = db.query(ResumeAttempt).filter(ResumeAttempt.run_id == int(run_id)).order_by(ResumeAttempt.id).all()
    result = {
        "event_count": len(events),
        "event_statuses": [str(event.status).lower() for event in events],
        "terminal_count": sum(bool(event.is_terminal) for event in events),
        "terminal_event_id": ledger.terminal_event_id,
        "authority_lifecycle": authority.lifecycle,
        "attempt_count": len(attempts),
        "attempt_statuses": [str(attempt.status).lower() for attempt in attempts],
    }
    print(json.dumps(result, separators=(",", ":")))
finally:
    db.close()
PY
)"
LEDGER_JSON="$LEDGER_JSON" "$PYTHON_BIN" - <<'PY'
import json, os, sys
ledger = json.loads(os.environ["LEDGER_JSON"])
statuses = set(ledger["event_statuses"])
if ledger["event_count"] < 2 or "blocked" not in statuses or "completed" not in statuses:
    raise SystemExit("ledger missing BLOCKED/COMPLETED event history")
if ledger["terminal_count"] != 1 or ledger["authority_lifecycle"] != "completed":
    raise SystemExit("ledger terminal latch is not unique/completed")
if ledger["attempt_count"] != 1 or ledger["attempt_statuses"] != ["completed"]:
    raise SystemExit("ledger missing completed resume authority/attempt")
if not ledger["terminal_event_id"]:
    raise SystemExit("ledger missing terminal event id")
PY

# Full Trace proof: send a bounded, synthetic trace over the official CSP
# data-plane HTTP endpoint, then read it back through the owner-authorized
# control-plane API.  Attributes contain only correlation/binding facts and a
# content hash; no bearer material, execution grant, or raw reasoning crosses
# this harness boundary.
FULL_TRACE_PAYLOAD="$(
  RUN_ID="$RUN_ID" TRACE_ID="$TRACE_ID" TASK_ID="$TASK_ID" RUN_DB_ID="$RUN_DB_ID" USER_ID="$USER_ID" \
  SOURCE_ID="$SOURCE_ID" INVOCATION_ID="$INVOCATION_ID" SESSION_ID="$SESSION_ID" \
  "$PYTHON_BIN" - <<'PY'
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

trace_id = os.environ["TRACE_ID"]
task_id = int(os.environ["TASK_ID"])
run_id = int(os.environ["RUN_DB_ID"])
owner_id = int(os.environ["USER_ID"])
source_snapshot_id = int(os.environ["SOURCE_ID"])
invocation_id = os.environ["INVOCATION_ID"]
session_id = os.environ["SESSION_ID"]
now = datetime.now(timezone.utc).replace(microsecond=0)
started_at = now.isoformat().replace("+00:00", "Z")
ended_at = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
content_sha256 = hashlib.sha256(
    f"{trace_id}|{task_id}|{run_id}|gate5-full-trace".encode("utf-8")
).hexdigest()
binding = {
    "trace_id": trace_id,
    "task_id": task_id,
    "run_id": run_id,
    "owner_id": owner_id,
    "source_snapshot_id": source_snapshot_id,
    "invocation_id": invocation_id,
    "session_id": session_id,
    "classification": "無機密",
}
spans = [
    {
        "span_id": f"gate5-fulltrace-run-{os.environ['RUN_ID']}",
        "trace_id": trace_id,
        "span_type": "agent.run.started",
        "name": "gate5.full_trace.run",
        "status": "ok",
        "started_at": started_at,
        "ended_at": ended_at,
        "classification_level": "無機密",
        "attributes": {
            "event_binding": binding,
            "content_kind": "synthetic_gate5_full_trace",
            "content_sha256": content_sha256,
        },
    },
    {
        "span_id": f"gate5-fulltrace-output-{os.environ['RUN_ID']}",
        "parent_span_id": f"gate5-fulltrace-run-{os.environ['RUN_ID']}",
        "trace_id": trace_id,
        "span_type": "agent.output.finished",
        "name": "gate5.full_trace.output",
        "status": "ok",
        "started_at": started_at,
        "ended_at": ended_at,
        "classification_level": "無機密",
        "attributes": {
            "event_binding": binding,
            "content_kind": "synthetic_gate5_full_trace",
            "content_sha256": content_sha256,
        },
    },
]
print(json.dumps({"spans": spans}, ensure_ascii=True, separators=(",", ":")))
PY
)"
FULL_TRACE_RESPONSE="$(csp_http_post \
  "/v1/traces/$TRACE_ID/spans" "$FULL_TRACE_PAYLOAD" '{}' "$PUBLIC_ACCESS_TOKEN")"
FULL_TRACE_ACCEPTED="$(FULL_TRACE_RESPONSE="$FULL_TRACE_RESPONSE" "$PYTHON_BIN" -c \
  'import json,os; print(json.loads(os.environ["FULL_TRACE_RESPONSE"]).get("accepted",-1))')"
FULL_TRACE_DUPLICATES="$(FULL_TRACE_RESPONSE="$FULL_TRACE_RESPONSE" "$PYTHON_BIN" -c \
  'import json,os; print(json.loads(os.environ["FULL_TRACE_RESPONSE"]).get("duplicates",-1))')"
if [[ "$FULL_TRACE_ACCEPTED" != "2" || "$FULL_TRACE_DUPLICATES" != "0" ]]; then
  echo "BLOCKED: Full Trace POST did not accept the complete synthetic batch" >&2
  exit 1
fi
unset FULL_TRACE_RESPONSE FULL_TRACE_PAYLOAD

FULL_TRACE_READBACK="$(csp_http_get "/api/traces/$TRACE_ID" "$PUBLIC_ACCESS_TOKEN")"
FULL_TRACE_READBACK="$FULL_TRACE_READBACK" TRACE_ID="$TRACE_ID" TASK_ID="$TASK_ID" \
RUN_DB_ID="$RUN_DB_ID" USER_ID="$USER_ID" SOURCE_ID="$SOURCE_ID" \
INVOCATION_ID="$INVOCATION_ID" SESSION_ID="$SESSION_ID" "$PYTHON_BIN" - <<'PY'
import json
import os

payload = json.loads(os.environ["FULL_TRACE_READBACK"])
if payload.get("trace_id") != os.environ["TRACE_ID"]:
    raise SystemExit("Full Trace readback trace_id mismatch")
if payload.get("task_id") != int(os.environ["TASK_ID"]):
    raise SystemExit("Full Trace readback task_id mismatch")
spans = payload.get("spans")
if not isinstance(spans, list) or len(spans) < 2:
    raise SystemExit("Full Trace readback missing required spans")
by_name = {span.get("name"): span for span in spans}
for name in ("gate5.full_trace.run", "gate5.full_trace.output"):
    if name not in by_name:
        raise SystemExit(f"Full Trace readback missing {name}")
selected = [by_name["gate5.full_trace.run"], by_name["gate5.full_trace.output"]]
expected = {
    "trace_id": os.environ["TRACE_ID"],
    "task_id": int(os.environ["TASK_ID"]),
    "run_id": int(os.environ["RUN_DB_ID"]),
    "owner_id": int(os.environ["USER_ID"]),
    "source_snapshot_id": int(os.environ["SOURCE_ID"]),
    "invocation_id": os.environ["INVOCATION_ID"],
    "session_id": os.environ["SESSION_ID"],
    "classification": "無機密",
}
for span in selected:
    if span.get("trace_id") != expected["trace_id"]:
        raise SystemExit("Full Trace span trace_id mismatch")
    if span.get("task_id") != expected["task_id"]:
        raise SystemExit("Full Trace span task_id mismatch")
    if span.get("producer") != "proxy":
        raise SystemExit("Full Trace span producer was not owner data-plane proxy")
    if span.get("classification_level") != expected["classification"]:
        raise SystemExit("Full Trace classification floor mismatch")
    binding = (span.get("attributes") or {}).get("event_binding")
    if binding != expected:
        raise SystemExit("Full Trace event binding mismatch")
    if (span.get("attributes") or {}).get("content_kind") != "synthetic_gate5_full_trace":
        raise SystemExit("Full Trace content kind mismatch")
    content_hash = (span.get("attributes") or {}).get("content_sha256")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise SystemExit("Full Trace content hash missing")
output_span = by_name["gate5.full_trace.output"]
run_span = by_name["gate5.full_trace.run"]
if output_span.get("parent_span_id") != run_span.get("span_id"):
    raise SystemExit("Full Trace parent span binding mismatch")
PY

echo "PASS: Gate 5 Silver official anila-agent dispatch -> BLOCKED -> restart -> resume -> replay; CSP ledger and Full Trace HTTP readback verified"
