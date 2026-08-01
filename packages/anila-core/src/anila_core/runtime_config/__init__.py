"""Runtime config parse / apply helpers (hot-reload poller retired).

Exposes:

  * :class:`RuntimeConfigSnapshot` — parsed shape of the admin-set
    JSON living in CSP's ``agents.runtime_config`` column.
  * :func:`parse_runtime_config` — tolerant JSON → typed parser.
  * :func:`apply_runtime_config` — mutate a :class:`ToolRegistry` (and
    surface guardrails / workspace caps) from a snapshot.

The former :class:`RuntimeConfigPoller` and its CSP poll target
(``GET /api/agents/me/runtime-config``) are removed: the official agent
template never started the poller, and admin writes already return 410.
Keeping a dead poller + auth'd poll endpoint would only exist to serve
long-lived agent ``csk-`` credentials — the opposite of the owner's
direction. ``parse`` / ``apply`` remain as library primitives if an
agent chooses to apply a snapshot from its own code.
"""

from .snapshot import (
    GuardrailSpec,
    PermissionSpec,
    RuntimeConfigSnapshot,
    WorkspaceSpec,
    parse_runtime_config,
)
from .apply import apply_runtime_config


__all__ = [
    "GuardrailSpec",
    "PermissionSpec",
    "RuntimeConfigSnapshot",
    "WorkspaceSpec",
    "parse_runtime_config",
    "apply_runtime_config",
]
