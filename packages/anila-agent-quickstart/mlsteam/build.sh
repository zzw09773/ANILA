#!/bin/bash
# Build the MLSteam lab image on a machine that has network and Docker.
# Wheel download uses the network. pip install inside the image does not:
# docker build --network=none, and the Dockerfile's pip RUN is --network=none.
set -euo pipefail

# 目標是 linux/amd64。ARM 建置機會讓 Docker 選 ARM 基底與 ARM wheels，
# tar 與 manifest 仍被標成 x86_64。這裡直接拒絕，並在下面的 docker 命令釘平台。
TARGET_PLATFORM="linux/amd64"
HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in
  x86_64|amd64) ;;
  *)
    echo "refusing to build ${TARGET_PLATFORM} lab image on ${HOST_ARCH}; wheels and the image would not be x86_64" >&2
    exit 1
    ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BUILD="$HERE/build"
WHEELHOUSE="$BUILD/wheelhouse"
VERSION=$(tr -d '[:space:]' < "$ROOT/IMAGE_VERSION")
IMAGE="anila-quickstart-lab:${VERSION}"
TAR="$BUILD/anila-quickstart-lab-${VERSION}-py313-linux-x86_64.tar"
MANIFEST="$BUILD/release-manifest.json"
STAMP_FILE="$BUILD/stamp"

BASE_IMAGE=$(awk '/^FROM / { print $2; exit }' "$HERE/Dockerfile")
case "$BASE_IMAGE" in
  *@sha256:*) ;;
  *)
    echo "Dockerfile FROM is not pinned by digest: $BASE_IMAGE" >&2
    exit 1
    ;;
esac
LOCAL_REF=$(docker image inspect python:3.13-slim --format '{{index .RepoDigests 0}}')
LOCAL_HASH=${LOCAL_REF##*@}
case "$BASE_IMAGE" in
  *"@$LOCAL_HASH") ;;
  *)
    echo "Dockerfile base $BASE_IMAGE does not match local python:3.13-slim ($LOCAL_REF)" >&2
    exit 1
    ;;
esac

mkdir -p "$BUILD"
STAMP=$(
  {
    printf '%s\n%s\n' "$VERSION" "$BASE_IMAGE"
    sha256sum \
      "$ROOT/requirements.lock" \
      "$HERE/requirements-jupyter.lock" \
      "$HERE/Dockerfile" \
      "$ROOT/run.sh" \
      "$ROOT/agent.py" \
      "$ROOT/server.py" \
      "$ROOT/platform_io.py" \
      "$ROOT/llm.py" \
      "$ROOT/IMAGE_VERSION" \
      "$ROOT/README.md"
  } | sha256sum | awk '{print $1}'
)

if [[ -f "$STAMP_FILE" && -f "$TAR" && -f "$MANIFEST" ]] \
  && [[ "$(cat "$STAMP_FILE")" == "$STAMP" ]] \
  && docker image inspect "$IMAGE" >/dev/null 2>&1; then
  CACHED_PLATFORM=$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')
  if [[ "$CACHED_PLATFORM" == "linux/amd64" ]]; then
    echo "lab image already matches this lock and source: $IMAGE"
    docker image inspect "$IMAGE" --format 'image_id={{.Id}} size_bytes={{.Size}}'
    exit 0
  fi
  echo "cached $IMAGE is ${CACHED_PLATFORM}, not linux/amd64; rebuilding" >&2
fi

mkdir -p "$WHEELHOUSE"
if ! python3 - "$WHEELHOUSE" "$ROOT/requirements.lock" "$HERE/requirements-jupyter.lock" <<'PY'
import hashlib, re, sys
from pathlib import Path

wheelhouse = Path(sys.argv[1])
lock_paths = [Path(p) for p in sys.argv[2:]]

def parse(path: Path) -> dict[str, set[str]]:
    logical = path.read_text(encoding="utf-8").replace("\\\r\n", " ").replace("\\\n", " ")
    reqs: dict[str, set[str]] = {}
    for line in logical.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") or "://" in stripped:
            raise SystemExit(f"{path.name} has a URL or option line: {stripped}")
        header = stripped.split(" --hash", 1)[0].split(";", 1)[0].strip()
        match = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9._+-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;,]+)",
            header,
        )
        if match is None:
            raise SystemExit(f"{path.name} has an unpinned line: {stripped}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", stripped))
        if not hashes:
            raise SystemExit(f"{path.name} missing hash for {name}")
        reqs.setdefault(name, set()).update(hashes)
    if not reqs:
        raise SystemExit(f"{path.name} has no requirements")
    return reqs

reqs: dict[str, set[str]] = {}
for path in lock_paths:
    for name, hashes in parse(path).items():
        reqs.setdefault(name, set()).update(hashes)
by_hash = {digest: name for name, hashes in reqs.items() for digest in hashes}
wheels = list(wheelhouse.glob("*.whl"))
if not wheels:
    raise SystemExit("empty")
found = {name: False for name in reqs}
for path in wheels:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    owner = by_hash.get(digest)
    if owner is None:
        raise SystemExit(f"extra {path.name}")
    found[owner] = True
missing = [name for name, ok in found.items() if not ok]
if missing:
    raise SystemExit("missing " + ",".join(missing))
print(f"reusing wheelhouse: {len(wheels)} wheels")
PY
then
  echo "downloading binary wheels for Python 3.13 manylinux x86_64"
  rm -rf "$WHEELHOUSE"
  mkdir -p "$WHEELHOUSE"
  docker run --rm --platform=linux/amd64 \
    -v "$ROOT/requirements.lock:/tmp/requirements.lock:ro" \
    -v "$HERE/requirements-jupyter.lock:/tmp/requirements-jupyter.lock:ro" \
    -v "$WHEELHOUSE:/wheelhouse" \
    "$BASE_IMAGE" \
    sh -c "python -m pip download --dest /wheelhouse --only-binary=:all: --require-hashes --no-cache-dir -r /tmp/requirements.lock && python -m pip download --dest /wheelhouse --only-binary=:all: --require-hashes --no-cache-dir -r /tmp/requirements-jupyter.lock"
fi

python3 - "$WHEELHOUSE" "$ROOT/requirements.lock" "$HERE/requirements-jupyter.lock" <<'PY'
import hashlib, re, sys
from pathlib import Path

wheelhouse = Path(sys.argv[1])
lock_paths = [Path(p) for p in sys.argv[2:]]

def parse(path: Path) -> dict[str, set[str]]:
    logical = path.read_text(encoding="utf-8").replace("\\\r\n", " ").replace("\\\n", " ")
    reqs: dict[str, set[str]] = {}
    for line in logical.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") or "://" in stripped:
            raise SystemExit(f"{path.name} has a URL or option line: {stripped}")
        header = stripped.split(" --hash", 1)[0].split(";", 1)[0].strip()
        match = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9._+-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;,]+)",
            header,
        )
        if match is None:
            raise SystemExit(f"{path.name} has an unpinned line: {stripped}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", stripped))
        if not hashes:
            raise SystemExit(f"{path.name} missing hash for {name}")
        reqs.setdefault(name, set()).update(hashes)
    if not reqs:
        raise SystemExit(f"{path.name} has no requirements")
    return reqs

reqs: dict[str, set[str]] = {}
for path in lock_paths:
    for name, hashes in parse(path).items():
        reqs.setdefault(name, set()).update(hashes)

by_hash: dict[str, str] = {}
for name, hashes in reqs.items():
    for digest in hashes:
        by_hash.setdefault(digest, name)

wheels = sorted(wheelhouse.glob("*.whl"))
extras = [p.name for p in wheelhouse.iterdir() if p.suffix not in {".whl"} and p.name != ".gitkeep"]
if extras:
    raise SystemExit("wheelhouse has non-wheel files: " + ", ".join(extras))
found = {name: False for name in reqs}
for path in wheels:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    owner = by_hash.get(digest)
    if owner is None:
        raise SystemExit(f"wheel hash is not in the lock: {path.name}")
    found[owner] = True
missing = sorted(name for name, ok in found.items() if not ok)
if missing:
    raise SystemExit("wheelhouse is missing locked packages: " + ", ".join(missing))
print(f"wheelhouse ok: {len(wheels)} wheels for {len(reqs)} locked packages")
PY

echo "building $IMAGE with --network=none --platform=linux/amd64"
docker build \
  --platform=linux/amd64 \
  --network=none \
  --pull=false \
  -f "$HERE/Dockerfile" \
  --build-arg "LAB_IMAGE_VERSION=$VERSION" \
  -t "$IMAGE" \
  "$ROOT"

BUILT_PLATFORM=$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')
if [[ "$BUILT_PLATFORM" != "linux/amd64" ]]; then
  echo "refusing to publish $IMAGE: built ${BUILT_PLATFORM}, want linux/amd64" >&2
  exit 1
fi

echo "exporting $TAR"
docker save -o "$TAR" "$IMAGE"

LOCK_HASH=$(sha256sum "$ROOT/requirements.lock" | awk '{print $1}')
JUPYTER_HASH=$(sha256sum "$HERE/requirements-jupyter.lock" | awk '{print $1}')
IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}')
IMAGE_SIZE=$(docker image inspect "$IMAGE" --format '{{.Size}}')
python3 - "$MANIFEST" "$VERSION" "$IMAGE_ID" "$IMAGE_SIZE" "$BASE_IMAGE" \
  "$LOCK_HASH" "$JUPYTER_HASH" "$(basename "$TAR")" <<'PY'
import json, sys
path, version, image_id, size, base, lock_hash, jupyter_hash, tar_name = sys.argv[1:]
payload = {
    "image_version": version,
    "image_digest": image_id,
    "image_size_bytes": int(size),
    "base_image": base,
    "requirements_lock_sha256": lock_hash,
    "jupyter_lock_sha256": jupyter_hash,
    "tar": tar_name,
    "install_network": "none",
    "target": "py313-linux-x86_64",
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
print(json.dumps(payload, indent=2))
PY
printf '%s\n' "$STAMP" > "$STAMP_FILE"
echo "built $IMAGE ($IMAGE_SIZE bytes)"
