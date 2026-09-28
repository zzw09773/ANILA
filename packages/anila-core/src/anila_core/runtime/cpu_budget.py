"""How many uvicorn workers this container should start.

Read the cgroup CPU quota when Docker limits the container. Otherwise use
the CPUs the process is allowed to run on. The formulas cap the result so
a 128-thread host does not spawn hundreds of interpreters.
"""

from __future__ import annotations

import os
from pathlib import Path


def host_cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def cgroup_cpus() -> int | None:
    """CPU quota as a whole number of CPUs, or None when the quota is unlimited."""
    v2 = _read(Path("/sys/fs/cgroup/cpu.max"))
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0] != "max":
            quota, period = int(parts[0]), int(parts[1])
            if period > 0 and quota > 0:
                return max(1, quota // period)
        if parts and parts[0] == "max":
            return None
    quota_raw = _read(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
    period_raw = _read(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    if quota_raw and period_raw:
        quota, period = int(quota_raw), int(period_raw)
        if quota < 0 or period <= 0:
            return None
        return max(1, quota // period)
    return None


def visible_cpus() -> int:
    host = host_cpus()
    quota = cgroup_cpus()
    if quota is None:
        return host
    return max(1, min(host, quota))


def csp_worker_count(cpus: int | None = None) -> int:
    """CSP proxies long-lived streams. Two workers per visible CPU overlap a
    stalled event loop, and the cap of 32 keeps a 128-thread host from
    starting hundreds of interpreters (each with its own database pool).
    The floor of 2 still overlaps one slow request on a 1-CPU quota.
    """
    n = visible_cpus() if cpus is None else max(1, int(cpus))
    return max(2, min(2 * n, 32))


def router_worker_count(cpus: int | None = None) -> int:
    """The router is also I/O bound, but every worker writes the same
    session SQLite file. Fewer processes than CSP, cap 8, floor 2.
    """
    n = visible_cpus() if cpus is None else max(1, int(cpus))
    return max(2, min(n, 8))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
