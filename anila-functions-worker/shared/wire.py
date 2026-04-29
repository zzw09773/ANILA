"""JSON-line wire protocol shared by worker-api and sandbox daemon.

The IPC channel is a Unix-domain socket on a shared docker volume. The
on-the-wire format is dead-simple JSON-per-line:

  client → server: one ``JobSpec`` JSON object, terminated by ``\n``
  server → client: one or more event JSON objects, each terminated by
                   ``\n``; conversation ends with a sentinel
                   ``{"type": "__done__", ...}`` event then close.

Why JSON lines instead of protobuf / something fancier:

  * Smallest possible attack surface — both sides parse with stdlib
    ``json``, no schema-dependent codegen
  * Subprocess wrapper (``runtime.py``) emits events directly to its
    stdout one-per-line, so daemon's job is just "tail subprocess
    stdout, forward to socket". No re-serialization.
  * Trivial to log, replay, redact

Everything stays in this module so the api / sandbox sides cannot drift.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ── JobSpec ─────────────────────────────────────────────────────────────


@dataclass
class JobSpec:
    """Everything the sandbox needs to run one Action invocation.

    Mode selection:

    * ``mode="exec"`` — run ``Action.action(body, **reserved)``; sandbox
      injects valves / user / metadata into the user-code namespace
    * ``mode="extract"`` — exercise schema introspection only; sandbox
      *deliberately* does NOT inject valves / user / metadata so the
      static-AST path stays the canonical one and any side-effects in
      module top-level code can't leak credentials
    """

    code: str
    body: dict
    valves: dict = field(default_factory=dict)
    user: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    mode: Literal["exec", "extract"] = "exec"

    def serialize(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def deserialize(cls, raw: str) -> "JobSpec":
        return cls(**json.loads(raw))


# ── Event envelope ──────────────────────────────────────────────────────


def encode_event(event: dict[str, Any]) -> str:
    """Serialize one event dict as a single line (LF-terminated)."""
    return json.dumps(event, ensure_ascii=False) + "\n"


def decode_line(line: str) -> dict[str, Any]:
    """Parse one wire line back into a dict."""
    return json.loads(line.rstrip("\n"))


# ── Sentinel constants ──────────────────────────────────────────────────

DONE_EVENT_TYPE = "__done__"
ERROR_EVENT_TYPE = "error"
