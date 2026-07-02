"""Typed snapshot of the admin-set ``agents.runtime_config`` JSON.

The JSON shape is open (admins may add keys ahead of agent code that
understands them) so the parser is tolerant: unknown keys are logged
at DEBUG and dropped, not raised. Known keys are coerced into typed
records so the apply layer never sees raw dicts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PermissionSpec:
    """Per-tool permission policy lifted from runtime_config.

    Mirrors :class:`ToolRegistry.set_allow_list` / ``set_deny_list``
    plus the ALLOW/ASK/DENY per-tool ``ToolPermission`` enum (Sprint 11).

    Fields:
      * ``allow_list``: tool names allowed (``["*"]`` = all). When
        empty, the registry's default is "all allowed".
      * ``deny_list``: tool names explicitly denied (overrides allow).
      * ``ask_tools``: tool names whose ``ToolDefinition.permission``
        should be flipped to ``ASK`` (interrupt surfaces a
        ``tool_approval`` interrupt). ``deny_tools`` flips to ``DENY``.
    """

    allow_list: tuple[str, ...] = field(default_factory=tuple)
    deny_list: tuple[str, ...] = field(default_factory=tuple)
    ask_tools: tuple[str, ...] = field(default_factory=tuple)
    deny_tools: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WorkspaceSpec:
    """Per-agent workspace capability overrides.

    Mirrors :class:`anila_core.workspace.caps.WorkspaceCaps`. Fields
    that are ``None`` here mean "leave the agent's compiled-in default
    alone"; explicit values override.
    """

    fs_read: Optional[bool] = None
    fs_write: Optional[bool] = None
    network: Optional[bool] = None
    exec_bash: Optional[bool] = None
    exec_python: Optional[bool] = None
    command_allowlist: Optional[tuple[str, ...]] = None
    max_exec_seconds: Optional[int] = None
    max_workspace_size_mb: Optional[int] = None


@dataclass(frozen=True)
class GuardrailSpec:
    """Single guardrail entry as parsed from the JSON.

    The apply layer turns these into the right
    :class:`InputGuardrail` / :class:`OutputGuardrail` instances.

    Fields:
      * ``side``: ``"input"`` or ``"output"``.
      * ``kind``: which built-in to instantiate
        (``"regex_block"`` / ``"max_length"``).
      * ``params``: kwargs forwarded to the built-in's ``__init__``.
      * ``tool``: ``"*"`` to attach to every registered tool, or a
        specific tool name.
    """

    side: str
    kind: str
    params: dict[str, Any]
    tool: str = "*"


@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    """Parsed view of the JSON the admin set on ``agents.runtime_config``.

    Construction is via :func:`parse_runtime_config` — instances are
    immutable so the apply layer can pass them around without
    defensive copies.
    """

    permissions: PermissionSpec = field(default_factory=PermissionSpec)
    workspace: WorkspaceSpec = field(default_factory=WorkspaceSpec)
    guardrails: tuple[GuardrailSpec, ...] = field(default_factory=tuple)
    etag: str = ""
    """Stable hash of the source JSON. The poller uses this to skip
    re-applying when the config hasn't changed since the last fetch."""

    @classmethod
    def empty(cls, *, etag: str = "") -> "RuntimeConfigSnapshot":
        return cls(etag=etag)

    @property
    def is_empty(self) -> bool:
        return (
            self.permissions == PermissionSpec()
            and self.workspace == WorkspaceSpec()
            and not self.guardrails
        )


def _as_str_tuple(value: Any, *, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        logger.debug(
            "runtime_config: %r expected list of strings, got %r — ignoring",
            key, type(value).__name__,
        )
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        else:
            logger.debug(
                "runtime_config: %r contains non-string %r — skipped",
                key, item,
            )
    return tuple(out)


def _parse_permissions(raw: Any) -> PermissionSpec:
    if not isinstance(raw, dict):
        return PermissionSpec()
    return PermissionSpec(
        allow_list=_as_str_tuple(raw.get("allow_list"), key="permissions.allow_list"),
        deny_list=_as_str_tuple(raw.get("deny_list"), key="permissions.deny_list"),
        ask_tools=_as_str_tuple(raw.get("ask_tools"), key="permissions.ask_tools"),
        deny_tools=_as_str_tuple(raw.get("deny_tools"), key="permissions.deny_tools"),
    )


def _parse_workspace(raw: Any) -> WorkspaceSpec:
    if not isinstance(raw, dict):
        return WorkspaceSpec()
    cmd_list = raw.get("command_allowlist")
    return WorkspaceSpec(
        fs_read=raw.get("fs_read") if isinstance(raw.get("fs_read"), bool) else None,
        fs_write=raw.get("fs_write") if isinstance(raw.get("fs_write"), bool) else None,
        network=raw.get("network") if isinstance(raw.get("network"), bool) else None,
        exec_bash=raw.get("exec_bash") if isinstance(raw.get("exec_bash"), bool) else None,
        exec_python=raw.get("exec_python") if isinstance(raw.get("exec_python"), bool) else None,
        command_allowlist=(
            _as_str_tuple(cmd_list, key="workspace.command_allowlist")
            if cmd_list is not None
            else None
        ),
        max_exec_seconds=(
            raw["max_exec_seconds"]
            if isinstance(raw.get("max_exec_seconds"), int)
            else None
        ),
        max_workspace_size_mb=(
            raw["max_workspace_size_mb"]
            if isinstance(raw.get("max_workspace_size_mb"), int)
            else None
        ),
    )


_VALID_GUARDRAIL_KINDS = {"regex_block", "max_length"}


def _parse_guardrails(raw: Any) -> tuple[GuardrailSpec, ...]:
    if not isinstance(raw, dict):
        return ()
    out: list[GuardrailSpec] = []
    for side in ("input", "output"):
        entries = raw.get(side)
        if entries is None:
            continue
        if not isinstance(entries, list):
            logger.debug(
                "runtime_config: guardrails.%s expected list, got %r — ignoring",
                side, type(entries).__name__,
            )
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind not in _VALID_GUARDRAIL_KINDS:
                logger.debug(
                    "runtime_config: unknown guardrail kind %r — skipped", kind
                )
                continue
            params = {
                k: v for k, v in entry.items() if k not in {"kind", "tool"}
            }
            tool = entry.get("tool", "*")
            if not isinstance(tool, str):
                tool = "*"
            out.append(
                GuardrailSpec(
                    side=side, kind=str(kind), params=params, tool=tool
                )
            )
    return tuple(out)


def parse_runtime_config(
    raw: Any, *, etag: str = ""
) -> RuntimeConfigSnapshot:
    """Parse the admin-set JSON into a typed snapshot.

    Tolerant: unknown top-level keys / wrong-shape fields are logged
    at DEBUG and dropped. ``raw=None`` and ``raw={}`` both return an
    empty snapshot — distinct semantics aren't represented here; the
    apply layer separately handles "is_empty".
    """
    if not isinstance(raw, dict):
        return RuntimeConfigSnapshot.empty(etag=etag)

    permissions = _parse_permissions(raw.get("tool_permissions"))
    workspace = _parse_workspace(raw.get("workspace"))
    guardrails = _parse_guardrails(raw.get("guardrails"))

    known = {"tool_permissions", "workspace", "guardrails"}
    unknown = set(raw.keys()) - known
    if unknown:
        logger.debug(
            "runtime_config: unknown top-level keys %s — ignored "
            "(forward-compat tolerance)",
            sorted(unknown),
        )

    return RuntimeConfigSnapshot(
        permissions=permissions,
        workspace=workspace,
        guardrails=guardrails,
        etag=etag,
    )
