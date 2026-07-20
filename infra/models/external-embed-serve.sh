#!/usr/bin/env bash
# Standalone external NV-Embed proxy lifecycle.
#
# This wrapper intentionally hard-codes the external-only compose file and a
# unique project name.  It does not accept a second -f file, --remove-orphans,
# or an alternate project name: those options could make cleanup touch the
# canonical anila-models project.  The shared anila-models-net is never owned
# or removed here; ensure-models-network.sh only verifies/creates it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/models/docker-compose.external-embed.yml"
PROJECT="anila-external-embed"
SHARED_NETWORK="anila-models-net"

# shellcheck disable=SC1091
source "$REPO_ROOT/infra/deployment/scripts/ensure-models-network.sh"

# Do not source the repo-root .env here: Compose already performs its normal
# interpolation, and shell-sourcing it would try to assign Bash read-only
# names such as UID on this host.  TRITON_GRPC_URL is intentionally explicit
# at the command boundary (``export TRITON_GRPC_URL=host:port``).

dc() {
  # Compose interpolates required variables even for ``down``.  A harmless
  # loopback placeholder keeps cleanup possible when the operator no longer
  # has the upstream target in their shell; ``up``/``verify`` still call
  # verify_target and reject anything non-canonical before using this helper.
  TRITON_GRPC_URL="${TRITON_GRPC_URL:-127.0.0.1:1}" \
    ANILA_EXTERNAL_EMBED_UPSTREAM_EGRESS_POLICY_ID="${ANILA_EXTERNAL_EMBED_UPSTREAM_EGRESS_POLICY_ID:-egress.embedding}" \
    ANILA_EXTERNAL_EMBED_STANDALONE="external-embed-only-v1" \
    docker compose --project-name "$PROJECT" --file "$COMPOSE_FILE" "$@"
}

fail_usage() {
  echo "usage: $0 {up|verify|render|smoke|status|down}" >&2
  exit 2
}

verify_target() {
  [[ -n "${TRITON_GRPC_URL:-}" ]] || {
    echo "TRITON_GRPC_URL must be set to the exact Triton gRPC host:port" >&2
    return 1
  }
  PYTHONPATH="$REPO_ROOT/infra/policy/gate5${PYTHONPATH:+:$PYTHONPATH}" \
    TRITON_GRPC_URL="$TRITON_GRPC_URL" \
    python3 - <<'PY'
from __future__ import annotations

import os

from check_deployment_egress import _normalise_external_target

raw = os.environ["TRITON_GRPC_URL"]
normalised = _normalise_external_target(raw)
if normalised is None or raw != normalised:
    raise SystemExit(
        "TRITON_GRPC_URL must already be canonical host:port; "
        f"expected {normalised!r}, got {raw!r}"
    )
PY
}

render_verified_compose() {
  verify_target
  local resolved
  resolved="$(dc config --format json)"
  python3 -c '
import json
import sys

doc = json.load(sys.stdin)
if doc.get("name") != "anila-external-embed":
    raise SystemExit("external overlay must resolve as project anila-external-embed")
services = doc.get("services", {})
if set(services) != {"nv-embed-proxy"}:
    raise SystemExit("external overlay must define only nv-embed-proxy")
proxy = services["nv-embed-proxy"]
if proxy.get("ports"):
    raise SystemExit("external embedding proxy must not publish host ports")
if "8000" not in {str(item) for item in proxy.get("expose", [])}:
    raise SystemExit("external embedding proxy must expose internal port 8000")
if any("triton" in name.lower() for name in services if name != "nv-embed-proxy"):
    raise SystemExit("external overlay must not resolve a local Triton service")
labels = proxy.get("labels", {})
required = {
    "com.anila.inference-role": "external-shim",
    "com.anila.provider-locality": "internal_shim",
    "com.anila.egress-network": "embedding-egress",
    "com.anila.upstream-locality": "external_governed",
    "com.anila.upstream-transport": "triton-grpc",
}
for key, expected in required.items():
    if labels.get(key) != expected:
        raise SystemExit(f"missing/incorrect label {key}={expected}")
target = proxy.get("environment", {}).get("TRITON_GRPC_URL", "")
if labels.get("com.anila.egress-target") != target:
    raise SystemExit("egress-target label must equal canonical TRITON_GRPC_URL")
if not labels.get("com.anila.upstream-egress-policy-id", "").strip():
    raise SystemExit("upstream egress policy id label is missing")
if labels.get("com.anila.standalone-marker") != "external-embed-only-v1":
    raise SystemExit("external overlay standalone marker is missing or incorrect")
if proxy.get("user") != "10001:10001":
    raise SystemExit("external embedding proxy must run as fixed non-root 10001:10001")
if proxy.get("read_only") is not True:
    raise SystemExit("external embedding proxy root filesystem must be read-only")
if "ALL" not in {str(item) for item in proxy.get("cap_drop", [])}:
    raise SystemExit("external embedding proxy must drop all Linux capabilities")
if "no-new-privileges:true" not in {
    str(item) for item in proxy.get("security_opt", [])
}:
    raise SystemExit("external embedding proxy must disable privilege acquisition")
tmpfs = {
    str(item).split(":", 1)[0]
    for item in proxy.get("tmpfs", [])
    if isinstance(item, (str, int, float))
}
if "/tmp" not in tmpfs:
    raise SystemExit("external embedding proxy must provide a writable /tmp tmpfs")
networks = doc.get("networks", {})
shared = networks.get("models", {})
if shared.get("external") is not True or shared.get("name") != "anila-models-net":
    raise SystemExit("models network must be the external anila-models-net")
egress = networks.get("embedding-egress", {})
if egress.get("driver") != "bridge" or egress.get("external") is True or egress.get("internal") is True:
    raise SystemExit("embedding-egress must be a project-owned non-internal bridge")
if set(proxy.get("networks", {})) != {"models", "embedding-egress"}:
    raise SystemExit("proxy must attach to shared models and dedicated egress networks")
' <<<"$resolved"
  printf '%s\n' "$resolved"
}

verify_compose() {
  render_verified_compose >/dev/null
}

smoke() {
  dc exec -T nv-embed-proxy python3 - <<'PY'
from __future__ import annotations

import json
import math
import urllib.request

health = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10)
if health.status != 200:
    raise SystemExit(f"health status={health.status}")
request = urllib.request.Request(
    "http://127.0.0.1:8000/v1/embeddings",
    data=json.dumps({"model": "nv-embed-v2", "input": ["gate5 smoke"]}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
if body.get("model") != "nv-embed-v2":
    raise SystemExit(f"unexpected model={body.get('model')!r}")
rows = body.get("data")
if not isinstance(rows, list) or len(rows) != 1:
    raise SystemExit(f"expected one embedding row, got {rows!r}")
embedding = rows[0].get("embedding") if isinstance(rows[0], dict) else None
if not isinstance(embedding, list) or len(embedding) != 4096:
    raise SystemExit(f"expected dimension 4096, got {len(embedding) if isinstance(embedding, list) else None}")
if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in embedding):
    raise SystemExit("embedding contains a non-finite/non-numeric value")
print(json.dumps({"health": "ok", "count": len(rows), "dim": len(embedding), "model": body["model"]}))
PY
}

case "${1:-}" in
  up)
    (( $# == 1 )) || fail_usage
    ensure_models_network "$SHARED_NETWORK"
    verify_compose
    # Deliberately no --remove-orphans: the unique project is the only scope
    # this wrapper may reconcile.
    dc up -d --build
    ;;
  verify)
    (( $# == 1 )) || fail_usage
    verify_compose
    ;;
  render)
    (( $# == 1 )) || fail_usage
    # This is the formal preflight boundary: emit the exact JSON that passed
    # the same standalone project/service/marker/target validation as `up`.
    render_verified_compose
    ;;
  smoke)
    (( $# == 1 )) || fail_usage
    smoke
    ;;
  status)
    (( $# == 1 )) || fail_usage
    dc ps
    ;;
  down)
    (( $# == 1 )) || fail_usage
    # No --volumes or --remove-orphans; this only removes this project's
    # container and project-owned embedding-egress bridge.
    dc down
    ;;
  *)
    fail_usage
    ;;
esac
