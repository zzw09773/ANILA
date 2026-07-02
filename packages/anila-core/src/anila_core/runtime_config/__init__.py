"""Sprint 13 PR A4 — runtime config hot-reload subsystem.

Exposes:

  * :class:`RuntimeConfigSnapshot` — parsed shape of the admin-set
    JSON living in CSP's ``agents.runtime_config`` column.
  * :func:`parse_runtime_config` — tolerant JSON → typed parser.
  * :func:`apply_runtime_config` — mutate a :class:`ToolRegistry` (and
    surface guardrails / workspace caps) from a snapshot.
  * :class:`RuntimeConfigPoller` — background asyncio task that polls
    CSP every 30 s and re-applies on change. ETag short-circuits
    re-application when the JSON hasn't changed since last poll.

Design notes
============

* Hot-reload boundary is **the next turn**, not mid-turn. A poll that
  lands during ``QueryEngine.run`` has no effect on that run; the
  next turn picks up the new permission lists / guardrails / caps.
  This avoids weird race conditions where a tool starts under one
  policy and finishes under another.
* "No override" (``runtime_config=None`` in CSP) means the agent uses
  whatever its code-level defaults are. The poller never overwrites
  those defaults itself; it only stores the snapshot for the apply
  step to use.
* Unknown JSON keys are tolerated — admins may set fields a deployed
  agent doesn't understand yet (forward-compat). Unknown keys are
  logged at DEBUG.
"""

from .snapshot import (
    GuardrailSpec,
    PermissionSpec,
    RuntimeConfigSnapshot,
    WorkspaceSpec,
    parse_runtime_config,
)
from .apply import apply_runtime_config
from .poller import RuntimeConfigPoller


__all__ = [
    "GuardrailSpec",
    "PermissionSpec",
    "RuntimeConfigSnapshot",
    "WorkspaceSpec",
    "parse_runtime_config",
    "apply_runtime_config",
    "RuntimeConfigPoller",
]
