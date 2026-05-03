"""Apply a parsed :class:`RuntimeConfigSnapshot` to live agent state.

Three independent surfaces:

  * **ToolRegistry permission lists** — ``set_allow_list`` / ``set_deny_list``
  * **Per-tool ToolPermission flag** — flips ``ALLOW`` ↔ ``ASK`` ↔
    ``DENY`` on registered ``ToolDefinition`` records based on the
    snapshot's ``ask_tools`` / ``deny_tools``.
  * **Per-tool guardrails** — installs guardrail instances on the
    matching ``ToolDefinition.input_guardrails`` /
    ``output_guardrails`` lists. Replaces (not appends) so removing a
    guardrail from the JSON also removes it from the live agent.

Workspace caps are NOT mutated here — they're per-workspace ephemeral
state. Apply layer surfaces them via :meth:`workspace_caps` so the
agent's tool factory can pick them up at workspace creation time.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..engine.guardrails import (
    InputGuardrail,
    MaxLengthOutput,
    OutputGuardrail,
    RegexBlockInput,
    RegexBlockOutput,
)
from ..models.tool import ToolDefinition, ToolPermission
from ..router.tool_router import ToolRegistry
from ..workspace.caps import WorkspaceCaps
from .snapshot import GuardrailSpec, RuntimeConfigSnapshot, WorkspaceSpec


logger = logging.getLogger(__name__)


def apply_runtime_config(
    snapshot: RuntimeConfigSnapshot,
    registry: ToolRegistry,
    *,
    base_workspace_caps: Optional[WorkspaceCaps] = None,
) -> WorkspaceCaps:
    """Apply ``snapshot`` to the live ``registry``; return effective caps.

    Returns the :class:`WorkspaceCaps` derived from
    ``base_workspace_caps`` overlaid with the snapshot's
    :class:`WorkspaceSpec` overrides. Pass that into your workspace
    factory; nothing in this function actually creates workspaces.

    Idempotent — calling twice with the same snapshot produces the
    same end state. Safe to invoke from a hot-reload poller without
    coordinating with in-flight tool calls (the per-tool fields are
    swapped atomically; the next ``ToolRegistry.execute`` sees the
    new value).
    """
    _apply_permissions(snapshot, registry)
    _apply_guardrails(snapshot, registry)
    return _resolve_workspace_caps(snapshot.workspace, base_workspace_caps)


def _apply_permissions(
    snapshot: RuntimeConfigSnapshot, registry: ToolRegistry
) -> None:
    perms = snapshot.permissions
    # Allow / deny lists — empty means "no override; keep registry default
    # (which is 'all allowed unless denied')". ``set_*_list`` accepts a
    # plain list and clears any prior pin.
    if perms.allow_list:
        registry.set_allow_list(list(perms.allow_list))
    else:
        registry.set_allow_list([])
    if perms.deny_list:
        registry.set_deny_list(list(perms.deny_list))
    else:
        registry.set_deny_list([])

    # Per-tool ASK / DENY flag. Anything NOT in either set falls back
    # to ALLOW so a poll that removes a name from ``ask_tools`` also
    # restores the tool to its default ALLOW state.
    ask = set(perms.ask_tools)
    deny = set(perms.deny_tools)
    overlap = ask & deny
    if overlap:
        logger.warning(
            "runtime_config: %s appear in both ask_tools and deny_tools "
            "— DENY wins for these.",
            sorted(overlap),
        )
    for tool_name in registry.list_tools():
        tool = registry.get_or_none(tool_name)
        if tool is None:
            continue
        if tool_name in deny:
            tool.permission = ToolPermission.DENY
        elif tool_name in ask:
            tool.permission = ToolPermission.ASK
        else:
            tool.permission = ToolPermission.ALLOW


def _build_input_guardrail(spec: GuardrailSpec) -> Optional[InputGuardrail]:
    if spec.kind == "regex_block":
        try:
            return RegexBlockInput(**spec.params)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "runtime_config: bad regex_block input params %r: %s",
                spec.params, exc,
            )
            return None
    logger.debug(
        "runtime_config: unknown input guardrail kind %r — skipped", spec.kind
    )
    return None


def _build_output_guardrail(spec: GuardrailSpec) -> Optional[OutputGuardrail]:
    if spec.kind == "regex_block":
        try:
            return RegexBlockOutput(**spec.params)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "runtime_config: bad regex_block output params %r: %s",
                spec.params, exc,
            )
            return None
    if spec.kind == "max_length":
        try:
            return MaxLengthOutput(**spec.params)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "runtime_config: bad max_length params %r: %s",
                spec.params, exc,
            )
            return None
    logger.debug(
        "runtime_config: unknown output guardrail kind %r — skipped", spec.kind
    )
    return None


def _apply_guardrails(
    snapshot: RuntimeConfigSnapshot, registry: ToolRegistry
) -> None:
    """Replace each tool's runtime-installed guardrails atomically.

    We use a sentinel marker on each ToolDefinition (``_runtime_*``
    list) so admin-installed guardrails are kept distinct from any
    code-defined ones. On each apply, the runtime list is rebuilt
    from the snapshot — code-defined guardrails attached at
    registration time are preserved.
    """
    # Bucket by tool name (with "*" expanding to every registered tool).
    input_by_tool: dict[str, list[InputGuardrail]] = {}
    output_by_tool: dict[str, list[OutputGuardrail]] = {}

    all_tools = registry.list_tools()
    for spec in snapshot.guardrails:
        targets = all_tools if spec.tool == "*" else [spec.tool]
        if spec.side == "input":
            built = _build_input_guardrail(spec)
            if built is None:
                continue
            for t in targets:
                input_by_tool.setdefault(t, []).append(built)
        elif spec.side == "output":
            built = _build_output_guardrail(spec)
            if built is None:
                continue
            for t in targets:
                output_by_tool.setdefault(t, []).append(built)

    for tool_name in all_tools:
        tool = registry.get_or_none(tool_name)
        if tool is None:
            continue
        # Strip the prior runtime-installed guardrails (anything we
        # tagged ``_runtime_marker``) and re-append fresh from this
        # snapshot. Anything without the marker is a code-defined
        # guardrail and stays put.
        tool.input_guardrails = [
            g for g in tool.input_guardrails
            if not getattr(g, "_runtime_marker", False)
        ]
        tool.output_guardrails = [
            g for g in tool.output_guardrails
            if not getattr(g, "_runtime_marker", False)
        ]
        for g in input_by_tool.get(tool_name, []):
            _tag_runtime(g)
            tool.input_guardrails.append(g)
        for g in output_by_tool.get(tool_name, []):
            _tag_runtime(g)
            tool.output_guardrails.append(g)


def _tag_runtime(obj: object) -> None:
    """Mark a guardrail instance as runtime-installed.

    Uses a sentinel attribute rather than a wrapper class so existing
    isinstance checks (``isinstance(g, InputGuardrail)``) still pass.
    """
    try:
        setattr(obj, "_runtime_marker", True)
    except Exception:  # pragma: no cover — frozen instances
        pass


def _resolve_workspace_caps(
    spec: WorkspaceSpec, base: Optional[WorkspaceCaps]
) -> WorkspaceCaps:
    """Overlay the snapshot's workspace overrides on the agent's base caps."""
    base_caps = base or WorkspaceCaps()
    overrides: dict[str, object] = {}
    if spec.fs_read is not None:
        overrides["fs_read"] = spec.fs_read
    if spec.fs_write is not None:
        overrides["fs_write"] = spec.fs_write
    if spec.network is not None:
        overrides["network"] = spec.network
    if spec.exec_bash is not None:
        overrides["exec_bash"] = spec.exec_bash
    if spec.exec_python is not None:
        overrides["exec_python"] = spec.exec_python
    if spec.command_allowlist is not None:
        overrides["command_allowlist"] = spec.command_allowlist
    if spec.max_exec_seconds is not None:
        overrides["max_exec_seconds"] = spec.max_exec_seconds
    if spec.max_workspace_size_mb is not None:
        overrides["max_workspace_size_mb"] = spec.max_workspace_size_mb
    if not overrides:
        return base_caps
    return base_caps.with_overrides(**overrides)
