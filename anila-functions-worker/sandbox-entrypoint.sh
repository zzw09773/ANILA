#!/bin/sh
# Sandbox container entrypoint.
#
# Executes as root (USER 0 in Dockerfile) so we can:
#   1. chown the named-volume mount point so sandbox + anila-jobs group
#      have rwx access (named volumes are root-owned by default)
#   2. setpriv down to `sandbox` user (uid 65533) carrying ambient
#      SETUID + SETGID capabilities so the daemon can spawn user-code
#      subprocess as `subproc` (uid 65534)
#
# CHOWN is left in `cap_add` at container level (see docker-compose.yml)
# but the entrypoint drops it from inheritance/ambient sets here, so
# the daemon doesn't have CHOWN at runtime — only at boot.
#
# `--no-new-privs` is sticky after this point: subprocesses can no
# longer gain privileges via setuid binaries or file capabilities,
# matching the docker `no-new-privileges:true` security_opt.

set -e

JOBS_DIR="${JOBS_DIR:-/jobs-exec}"

if [ ! -d "$JOBS_DIR" ]; then
    mkdir -p "$JOBS_DIR"
fi

# Order matters: chmod BEFORE chown.
#
# After chown changes the directory's owner from root to `sandbox`,
# root no longer "owns" the dir. With cap_drop:ALL we lack CAP_FOWNER,
# so a subsequent chmod by root on a non-root-owned directory hits
# EPERM. Doing chmod first (while still root-owned, no cap needed)
# then chown-ing avoids the issue without needing to add CAP_FOWNER
# to the container.
chmod 0770 "$JOBS_DIR"
chown sandbox:anila-jobs "$JOBS_DIR"

# Hand off to the daemon as `sandbox` user with ambient SETUID/SETGID.
# CHOWN/FOWNER intentionally NOT in --inh-caps / --ambient-caps so the
# daemon can't re-chown / re-permission anything (those caps were only
# needed by this entrypoint).
#
# Run via `python -m sandbox.daemon` with PYTHONPATH=/app so the
# package import (`from sandbox.ambient ...`) resolves; if we just say
# `python /app/sandbox/daemon.py`, Python prepends /app/sandbox/ to
# sys.path which doesn't help the package-style imports.
exec setpriv \
    --reuid=sandbox \
    --regid=sandbox \
    --init-groups \
    --no-new-privs \
    --inh-caps=+setuid,+setgid \
    --ambient-caps=+setuid,+setgid \
    -- env PYTHONPATH=/app python -u -m sandbox.daemon
