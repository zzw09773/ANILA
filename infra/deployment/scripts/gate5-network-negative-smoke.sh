#!/usr/bin/env bash
# Gate 5 live Docker network-negative smoke.
#
# The fixture intentionally uses direct container IP/TCP probes.  It does not
# rely on Docker DNS, and it never publishes a host port.  A CSP-like client is
# dual-homed (platform + internal model bridge); Router/worker/Studio/Agent and
# an untrusted platform client are platform-only and must fail to reach the
# mock model.  The same assertions run after restart and network recreation.
set -euo pipefail

IMAGE="${GATE5_SMOKE_IMAGE:-python:3.11-slim}"
SUFFIX="${GATE5_SMOKE_SUFFIX:-$$}"
MODEL_NET="anila-gate5-smoke-models-${SUFFIX}"
PLATFORM_NET="anila-gate5-smoke-platform-${SUFFIX}"
MODEL_CONTAINER="anila-gate5-smoke-model-${SUFFIX}"
CSP_CONTAINER="anila-gate5-smoke-csp-${SUFFIX}"
CLIENT_ROLES=(router worker studio agent untrusted)
CLIENT_CONTAINERS=()

log() { printf '[gate5-network] %s\n' "$*"; }
fail() { printf '[gate5-network] FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
  set +e
  local name
  for name in "$MODEL_CONTAINER" "$CSP_CONTAINER" "${CLIENT_CONTAINERS[@]}"; do
    [[ -n "$name" ]] && docker rm -f "$name" >/dev/null 2>&1
  done
  docker network rm "$MODEL_NET" "$PLATFORM_NET" >/dev/null 2>&1
}
trap cleanup EXIT

require_internal_bridge() {
  local network="$1" expected_internal="$2" internal driver
  internal="$(docker network inspect "$network" --format '{{.Internal}}' 2>/dev/null || true)"
  driver="$(docker network inspect "$network" --format '{{.Driver}}' 2>/dev/null || true)"
  [[ "$internal" == "$expected_internal" && "$driver" == "bridge" ]] || \
    fail "$network topology mismatch: Internal=${internal:-missing} Driver=${driver:-missing}"
}

create_networks() {
  docker network create --driver bridge --internal "$MODEL_NET" >/dev/null
  docker network create --driver bridge "$PLATFORM_NET" >/dev/null
  require_internal_bridge "$MODEL_NET" true
  require_internal_bridge "$PLATFORM_NET" false
}

start_fixture() {
  local role name
  docker run -d --name "$MODEL_CONTAINER" --network "$MODEL_NET" \
    "$IMAGE" python -u -c \
    'import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"mock-model")
    def log_message(self, *_): pass
http.server.HTTPServer(("0.0.0.0", 8080), H).serve_forever()' \
    >/dev/null

  docker run -d --name "$CSP_CONTAINER" --network "$PLATFORM_NET" \
    "$IMAGE" python -c 'import time; time.sleep(3600)' >/dev/null
  docker network connect "$MODEL_NET" "$CSP_CONTAINER"

  for role in "${CLIENT_ROLES[@]}"; do
    name="anila-gate5-smoke-${role}-${SUFFIX}"
    CLIENT_CONTAINERS+=("$name")
    docker run -d --name "$name" --network "$PLATFORM_NET" \
      "$IMAGE" python -c 'import time; time.sleep(3600)' >/dev/null
  done

  local published
  for name in "$MODEL_CONTAINER" "$CSP_CONTAINER" "${CLIENT_CONTAINERS[@]}"; do
    published="$(docker port "$name" 2>/dev/null || true)"
    [[ -z "$published" ]] || fail "$name unexpectedly publishes host port: $published"
  done
}

model_ip() {
  docker inspect -f "{{with index .NetworkSettings.Networks \"$MODEL_NET\"}}{{.IPAddress}}{{end}}" \
    "$MODEL_CONTAINER"
}

probe_http() {
  local container="$1" ip="$2"
  docker exec "$container" python -c '
import socket, sys
sock = socket.create_connection((sys.argv[1], 8080), timeout=2)
sock.sendall(b"GET / HTTP/1.0\r\nHost: mock-model\r\n\r\n")
data = sock.recv(256)
sock.close()
if b"200" not in data:
    raise SystemExit(f"unexpected model response: {data!r}")
' "$ip"
}

probe_suite() {
  local phase="$1" ip="$2" role container
  log "$phase: direct-IP positive/negative probes"
  probe_http "$CSP_CONTAINER" "$ip" || fail "$phase CSP-like dual-homed client cannot reach mock model"
  for role in "${CLIENT_ROLES[@]}"; do
    container="anila-gate5-smoke-${role}-${SUFFIX}"
    if probe_http "$container" "$ip" >/dev/null 2>&1; then
      fail "$phase $role unexpectedly reached model IP/TCP"
    fi
  done
}

wait_for_model() {
  local ip="$1" attempt
  # python:3.11-slim is already local in CI/dev, but a cold Docker Desktop
  # daemon can still take more than 30 seconds to start the first fixture.
  for attempt in $(seq 1 60); do
    if probe_http "$CSP_CONTAINER" "$ip" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  docker logs "$MODEL_CONTAINER" >&2 || true
  fail "mock model did not become reachable"
}

main() {
  docker info >/dev/null 2>&1 || fail "Docker daemon unavailable"
  docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "missing local smoke image: $IMAGE"
  log "using image $IMAGE"

  create_networks
  start_fixture
  local ip
  ip="$(model_ip)"
  [[ -n "$ip" ]] || fail "mock model has no model-network IP"
  wait_for_model "$ip"
  probe_suite initial "$ip"

  log "restart phase"
  docker restart --timeout 0 "$MODEL_CONTAINER" "$CSP_CONTAINER" "${CLIENT_CONTAINERS[@]}" >/dev/null
  ip="$(model_ip)"
  wait_for_model "$ip"
  probe_suite restart "$ip"

  log "recreate phase"
  docker rm -f "$MODEL_CONTAINER" "$CSP_CONTAINER" "${CLIENT_CONTAINERS[@]}" >/dev/null
  CLIENT_CONTAINERS=()
  docker network rm "$MODEL_NET" "$PLATFORM_NET" >/dev/null
  create_networks
  start_fixture
  ip="$(model_ip)"
  wait_for_model "$ip"
  probe_suite recreate "$ip"
  log "PASS: internal model bridge + dual-home positive + platform-only negatives survived restart/recreate"
}

main "$@"
