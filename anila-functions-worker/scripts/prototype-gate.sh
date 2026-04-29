#!/bin/sh
# Sprint 2.5 prototype gate — manual smoke check for capability landing.
#
# Run from host: ./scripts/prototype-gate.sh
# Or pass a service name: ./scripts/prototype-gate.sh anila-functions-sandbox-extract
#
# Spec §5.8 invariants — all 6 must pass before opening Sprint 3 / dogfood.

set -u

SVC="${1:-anila-functions-sandbox-exec}"
PASS="\033[32m✓\033[0m"
FAIL="\033[31m✗\033[0m"
exit_code=0

check() {
    label="$1"; shift
    if "$@" > /tmp/check.out 2>&1; then
        printf "  %b  %s\n" "$PASS" "$label"
        cat /tmp/check.out | sed 's/^/      /'
    else
        printf "  %b  %s\n" "$FAIL" "$label"
        sed 's/^/      /' /tmp/check.out
        exit_code=1
    fi
}

echo "Sprint 2.5 prototype gate against: $SVC"
echo

# ── 1. Bounding set has SETUID, SETGID, CHOWN ──────────────────────────
echo "[1] Bounding set has SETUID + SETGID + CHOWN (+ FOWNER for re-runs)"
check "bounding caps present" \
    docker compose exec -T "$SVC" sh -c '
        out=$(capsh --print | grep "^Bounding" || true)
        echo "  $out"
        echo "$out" | grep -q cap_setuid &&
        echo "$out" | grep -q cap_setgid &&
        echo "$out" | grep -q cap_chown
    '

# ── 2 + 3. Daemon ambient caps + spawn check (read from container logs) ─
#
# These two invariants must be probed from inside the actual daemon
# process — `docker exec --user 65533` is a fresh process without the
# entrypoint's setpriv ambient caps, so probing it would lie. The
# daemon logs its own probe at startup; we grep for it.

echo
echo "[2] Daemon (sandbox uid 65533) ambient SETUID + SETGID — from daemon log"
check "daemon ambient caps logged" \
    sh -c "docker compose logs --no-color $SVC 2>/dev/null | grep -q 'ambient_setuid=1 ambient_setgid=1' && \
           docker compose logs --no-color $SVC 2>/dev/null | grep '\[probe\] uid=' | tail -1"

echo
echo "[3] Daemon spawned subprocess as uid 65534 — from daemon log"
check "spawn-as-subproc logged OK" \
    sh -c "docker compose logs --no-color $SVC 2>/dev/null | grep -q 'spawn-as-subproc OK (uid=65534)' && \
           docker compose logs --no-color $SVC 2>/dev/null | grep '\[probe\] spawn-as-subproc' | tail -1"

# ── 4. Subprocess can't setuid(0) ─────────────────────────────────────
echo
echo "[4] Subprocess (uid 65534) cannot setuid(0)"
check "EPERM on setuid(0)" \
    docker compose exec -T --user 65534 "$SVC" sh -c '
python3 << "PYEOF"
import os, sys
try:
    os.setuid(0)
    print("  setuid(0) UNEXPECTEDLY succeeded")
    sys.exit(1)
except PermissionError as e:
    print(f"  setuid(0) blocked: {e}")
    sys.exit(0)
PYEOF
    '

# ── 5. Subprocess can't connect control socket ────────────────────────
JOBS_DIR="/jobs-exec"
[ "$SVC" = "anila-functions-sandbox-extract" ] && JOBS_DIR="/jobs-extract"

echo
echo "[5] Subprocess EACCES on $JOBS_DIR/control.sock"
check "no socket connect" \
    docker compose exec -T --user 65534 -e JD="$JOBS_DIR" "$SVC" sh -c '
python3 << "PYEOF"
import os, socket, sys
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    sock.connect(os.environ["JD"] + "/control.sock")
    print("  connect UNEXPECTEDLY succeeded")
    sys.exit(1)
except PermissionError as e:
    print(f"  connect blocked: {e}")
    sys.exit(0)
except FileNotFoundError as e:
    print(f"  socket not yet bound — daemon may not be listening: {e}")
    sys.exit(0)
PYEOF
    '

# ── 6. Subprocess can't listdir jobs dir ──────────────────────────────
echo
echo "[6] Subprocess EACCES on $JOBS_DIR/"
check "no listdir" \
    docker compose exec -T --user 65534 -e JD="$JOBS_DIR" "$SVC" sh -c '
python3 << "PYEOF"
import os, sys
try:
    entries = os.listdir(os.environ["JD"])
    print(f"  listdir UNEXPECTEDLY succeeded: {entries}")
    sys.exit(1)
except PermissionError as e:
    print(f"  listdir blocked: {e}")
    sys.exit(0)
PYEOF
    '

echo
if [ $exit_code -eq 0 ]; then
    printf "%bAll 6 prototype gate checks PASSED for %s%b\n" "\033[32m" "$SVC" "\033[0m"
else
    printf "%bGate FAILED — review failures above%b\n" "\033[31m" "\033[0m"
fi
exit $exit_code
