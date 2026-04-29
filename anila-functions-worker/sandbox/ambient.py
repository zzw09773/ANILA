"""Clear ambient capabilities before exec'ing user subprocess.

The sandbox daemon is started by ``sandbox-entrypoint.sh`` via
``setpriv --ambient-caps=+setuid,+setgid``, so the daemon runs with
SETUID + SETGID in its ambient set. ``subprocess.Popen(user='subproc',
group='subproc')`` therefore succeeds (kernel sees effective SETUID).

But Linux passes the parent's ambient set through ``execve()``, which
means the *child* (user code) would inherit SETUID/SETGID too. That's
exactly what we don't want — the child should be a regular non-root
process with no ability to change UID.

Solution: wrap the spawn with a ``preexec_fn`` that calls
``prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL)`` after fork but
before exec. The clear happens in the child's pre-exec address space
so the child starts with an empty ambient set.

Combined with the docker-level ``no-new-privileges:true`` and the
remaining ``cap_drop:ALL``, the user subprocess can't escalate:

  * ambient set: empty (this module clears it)
  * permitted set: empty (cap_drop:ALL at container level)
  * effective set: empty (computed from permitted ∩ ambient)
  * no_new_privs: prevents setuid binaries / file caps from re-adding
    capabilities via subsequent execs
"""

from __future__ import annotations

import ctypes
import ctypes.util


# Linux-specific prctl constants from <linux/prctl.h>
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4


_libc = ctypes.CDLL(
    ctypes.util.find_library("c") or "libc.so.6", use_errno=True
)


def clear_ambient_caps() -> None:
    """Drop every capability from the calling process's ambient set.

    Used as ``preexec_fn`` for ``subprocess.Popen`` so the user
    subprocess inherits an empty ambient set despite the daemon
    needing SETUID/SETGID for the spawn itself.

    Raises :class:`OSError` on prctl failure (most likely on a kernel
    older than 4.3 that lacks ``PR_CAP_AMBIENT``). Sandbox daemon
    should treat this as fatal — without ambient clear, isolation is
    broken.
    """
    rc = _libc.prctl(
        PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0
    )
    if rc != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl PR_CAP_AMBIENT_CLEAR_ALL failed (errno={errno})")
