#!/bin/bash
# Exercise the lab image the way a MLSteam lab runs: one container, no Docker
# inside it. The service is started with ./run.sh. CSP is unreachable on purpose.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
REPO=$(cd "$ROOT/../.." && pwd)
VERSION=$(tr -d '[:space:]' < "$ROOT/IMAGE_VERSION")
IMAGE="anila-quickstart-lab:${VERSION}"
CID="anila-quickstart-lab-smoke-$$"
VERIFIER="$REPO/packages/anila-core/src/anila_core/contrib/anila_verify.py"

fail() {
  echo "smoke FAIL: $*" >&2
  exit 1
}

cleanup() {
  docker rm -f "$CID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

bash "$HERE/build.sh"

docker image inspect "$IMAGE" >/dev/null
arch=$(docker image inspect "$IMAGE" --format '{{.Architecture}}')
[[ "$arch" == "amd64" ]] || fail "image architecture is ${arch}, want amd64"

id_out=$(docker run --rm --network=none --entrypoint id "$IMAGE")
echo "$id_out" | grep -q 'uid=10001' || fail "image is not the non-root lab user: $id_out"

img_ver=$(docker run --rm --network=none --entrypoint cat "$IMAGE" /etc/anila/lab-image-version | tr -d '[:space:]')
env_ver=$(docker run --rm --network=none --entrypoint printenv "$IMAGE" ANILA_LAB_IMAGE_VERSION | tr -d '[:space:]')
[[ "$img_ver" == "$VERSION" && "$env_ver" == "$VERSION" ]] || fail "image version $img_ver/$env_ver != $VERSION"

echo "pip check"
docker run --rm --network=none --entrypoint python "$IMAGE" -m pip check

STAGE=$(mktemp -d)
python3 - "$STAGE/ca.pem" <<'PY'
import datetime as dt
import sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "smoke-ca")])
now = dt.datetime.now(dt.timezone.utc)
cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(1)
    .not_valid_before(now)
    .not_valid_after(now + dt.timedelta(days=1))
    .sign(key, hashes.SHA256())
)
open(sys.argv[1], "wb").write(cert.public_bytes(serialization.Encoding.PEM))
PY
cat > "$STAGE/deployment.env" <<'EOF'
CSP_BASE_URL=https://127.0.0.1:9
ANILA_CA_FILE=/app/ca.pem
ANILA_AGENT_ID=42
LLM_BASE_URL=https://127.0.0.1:9/v1
LLM_MODEL=smoke-model
LLM_AUTH_REQUIRED=false
EOF
python3 - "$STAGE/bundle.json" "$VERSION" <<'PY'
import json, sys
json.dump({"compatible_lab_image_version": sys.argv[2]}, open(sys.argv[1], "w"), indent=2)
open(sys.argv[1], "a").write("\n")
PY
cp "$VERIFIER" "$STAGE/anila_verify.py"

HOST_PORT=$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)

docker run -d --name "$CID" \
  --user 10001:10001 \
  -p "127.0.0.1:${HOST_PORT}:8200" \
  -w /app \
  "$IMAGE" \
  sleep infinity >/dev/null

docker cp "$STAGE/deployment.env" "$CID":/app/deployment.env
docker cp "$STAGE/bundle.json" "$CID":/app/bundle.json
docker cp "$STAGE/ca.pem" "$CID":/app/ca.pem
docker cp "$STAGE/anila_verify.py" "$CID":/app/anila_verify.py
rm -rf "$STAGE"

docker exec "$CID" /app/run.sh start | grep -q 'status: running' || fail "start did not report running"
docker exec "$CID" /app/run.sh status | grep -q 'status: running' || fail "status did not report running"

BODY=$(mktemp)
health_code() {
  curl -sS -o "$BODY" -w '%{http_code}' --max-time 15 "http://127.0.0.1:${HOST_PORT}/health" || true
}

code=""
for _ in $(seq 1 40); do
  code=$(health_code)
  if [[ "$code" == "503" ]]; then
    break
  fi
  sleep 0.5
done
[[ "$code" == "503" ]] || fail "GET /health returned ${code:-none}, body=$(cat "$BODY" 2>/dev/null || true)"
if grep -q '"status": *"ok"' "$BODY"; then
  fail "GET /health was ready: $(cat "$BODY")"
fi

old=$(docker exec "$CID" cat /app/.anila-agent.uvicorn.pid | tr -d '[:space:]')
[[ -n "$old" ]] || fail "uvicorn pid file empty"
# The slim image has no kill(1). bash provides it.
docker exec "$CID" bash -c "kill $old" || fail "could not kill uvicorn $old"

new=""
for _ in $(seq 1 40); do
  new=$(docker exec "$CID" cat /app/.anila-agent.uvicorn.pid 2>/dev/null | tr -d '[:space:]' || true)
  if [[ -n "$new" && "$new" != "$old" ]]; then
    if docker exec "$CID" bash -c "kill -0 $new" 2>/dev/null; then
      break
    fi
  fi
  sleep 0.5
done
[[ -n "$new" && "$new" != "$old" ]] || fail "uvicorn was not restarted (old=$old new=${new:-none})"

code=""
for _ in $(seq 1 40); do
  code=$(health_code)
  if [[ "$code" == "503" ]]; then
    break
  fi
  sleep 0.5
done
[[ "$code" == "503" ]] || fail "health after restart returned ${code:-none}, body=$(cat "$BODY" 2>/dev/null || true)"
rm -f "$BODY"

start_s=$(date +%s)
stop_out=$(docker exec "$CID" /app/run.sh stop)
end_s=$(date +%s)
elapsed=$((end_s - start_s))
echo "$stop_out" | grep -q 'status: stopped' || fail "stop output: $stop_out"
echo "$stop_out" | grep -q 'forced' && fail "stop was forced: $stop_out"
[[ "$elapsed" -le 30 ]] || fail "stop took ${elapsed}s"
status_out=$(docker exec "$CID" /app/run.sh status || true)
echo "$status_out" | grep -q 'status: stopped' || fail "still running after stop: $status_out"

echo "smoke ok: image=$IMAGE health=503 restart=$old->$new stop=${elapsed}s"
