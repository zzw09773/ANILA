"""Tests for Sprint 13 PR A4 — runtime config parser + apply + poller."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from anila_core.engine.guardrails import (
    InputGuardrail,
    MaxLengthOutput,
    OutputGuardrail,
    RegexBlockInput,
    RegexBlockOutput,
)
from anila_core.models.tool import (
    ToolDefinition,
    ToolPermission,
    ToolSafety,
)
from anila_core.router.tool_router import ToolRegistry
from anila_core.runtime_config import (
    GuardrailSpec,
    PermissionSpec,
    RuntimeConfigPoller,
    RuntimeConfigSnapshot,
    WorkspaceSpec,
    apply_runtime_config,
    parse_runtime_config,
)
from anila_core.workspace.caps import WorkspaceCaps


# ---------------------------------------------------------------------------
# parse_runtime_config
# ---------------------------------------------------------------------------


def test_parse_none_returns_empty() -> None:
    snap = parse_runtime_config(None)
    assert snap.is_empty
    assert snap.permissions == PermissionSpec()
    assert snap.workspace == WorkspaceSpec()
    assert snap.guardrails == ()


def test_parse_empty_dict_returns_empty() -> None:
    assert parse_runtime_config({}).is_empty


def test_parse_full_config_round_trip() -> None:
    raw = {
        "tool_permissions": {
            "allow_list": ["*"],
            "deny_list": ["exec_bash"],
            "ask_tools": ["exec_python"],
            "deny_tools": ["file_write"],
        },
        "workspace": {
            "fs_read": True,
            "fs_write": False,
            "network": False,
            "exec_bash": False,
            "exec_python": True,
            "command_allowlist": ["ls", "cat"],
            "max_exec_seconds": 60,
            "max_workspace_size_mb": 256,
        },
        "guardrails": {
            "input": [
                {
                    "kind": "regex_block",
                    "pattern": r"sk-\w+",
                    "mode": "redact",
                    "tool": "*",
                }
            ],
            "output": [
                {"kind": "max_length", "max_chars": 4096},
                {
                    "kind": "regex_block",
                    "pattern": "secret",
                    "mode": "reject",
                    "tool": "exec_python",
                },
            ],
        },
    }
    snap = parse_runtime_config(raw, etag="abc123")
    assert snap.etag == "abc123"
    assert snap.permissions.allow_list == ("*",)
    assert snap.permissions.deny_list == ("exec_bash",)
    assert snap.permissions.ask_tools == ("exec_python",)
    assert snap.permissions.deny_tools == ("file_write",)
    assert snap.workspace.fs_write is False
    assert snap.workspace.command_allowlist == ("ls", "cat")
    assert snap.workspace.max_workspace_size_mb == 256
    assert len(snap.guardrails) == 3
    assert snap.guardrails[0].side == "input"
    assert snap.guardrails[0].kind == "regex_block"
    assert snap.guardrails[1].kind == "max_length"
    assert snap.guardrails[2].tool == "exec_python"


def test_parse_unknown_keys_tolerated() -> None:
    raw = {
        "tool_permissions": {"allow_list": ["*"], "future_field": "ignored"},
        "future_top_level": {"x": 1},
        "guardrails": {
            "input": [{"kind": "totally_made_up", "x": 1}],
        },
    }
    snap = parse_runtime_config(raw)
    assert snap.permissions.allow_list == ("*",)
    assert snap.guardrails == ()  # unknown kind dropped


def test_parse_handles_wrong_value_types() -> None:
    """Wrong-type fields (e.g. list where dict expected) get dropped, not raised."""
    raw = {
        "tool_permissions": "not-a-dict",
        "workspace": ["also-wrong"],
        "guardrails": {"input": "not-a-list"},
    }
    snap = parse_runtime_config(raw)
    assert snap.is_empty


def test_parse_workspace_partial_overrides_keep_others_none() -> None:
    raw = {"workspace": {"fs_read": False, "max_exec_seconds": 5}}
    snap = parse_runtime_config(raw)
    assert snap.workspace.fs_read is False
    assert snap.workspace.max_exec_seconds == 5
    assert snap.workspace.fs_write is None
    assert snap.workspace.network is None


# ---------------------------------------------------------------------------
# apply_runtime_config — permissions
# ---------------------------------------------------------------------------


def _make_tool(name: str) -> ToolDefinition:
    async def impl(_input, **_kw):
        return "ok"

    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
    )


def test_apply_permissions_sets_allow_and_deny_lists() -> None:
    reg = ToolRegistry()
    reg.register(_make_tool("a"))
    reg.register(_make_tool("b"))
    snap = RuntimeConfigSnapshot(
        permissions=PermissionSpec(
            allow_list=("a",), deny_list=("b",),
        )
    )
    apply_runtime_config(snap, reg)
    assert reg.can_use("a") is True
    assert reg.can_use("b") is False


def test_apply_permissions_flips_ask_and_deny_tool_flags() -> None:
    reg = ToolRegistry()
    reg.register(_make_tool("read_only"))
    reg.register(_make_tool("dangerous"))
    reg.register(_make_tool("default"))

    snap = RuntimeConfigSnapshot(
        permissions=PermissionSpec(
            ask_tools=("read_only",),
            deny_tools=("dangerous",),
        )
    )
    apply_runtime_config(snap, reg)

    assert reg.get("read_only").permission == ToolPermission.ASK
    assert reg.get("dangerous").permission == ToolPermission.DENY
    assert reg.get("default").permission == ToolPermission.ALLOW


def test_apply_permissions_resets_to_allow_when_removed() -> None:
    """Re-applying a snapshot that no longer mentions a tool restores ALLOW."""
    reg = ToolRegistry()
    reg.register(_make_tool("t"))

    snap1 = RuntimeConfigSnapshot(
        permissions=PermissionSpec(ask_tools=("t",))
    )
    apply_runtime_config(snap1, reg)
    assert reg.get("t").permission == ToolPermission.ASK

    snap2 = RuntimeConfigSnapshot()  # empty
    apply_runtime_config(snap2, reg)
    assert reg.get("t").permission == ToolPermission.ALLOW


def test_apply_permissions_empty_lists_clear_pin() -> None:
    """Empty allow/deny lists reset the registry's pin so the default
    'all allowed' policy applies again."""
    reg = ToolRegistry()
    reg.register(_make_tool("a"))
    reg.set_allow_list(["a"])  # pre-pinned to only allow 'a'

    snap = RuntimeConfigSnapshot()
    apply_runtime_config(snap, reg)
    assert reg.can_use("a") is True
    # An unregistered tool isn't allowed (registry doesn't know it),
    # but the deny list at least is empty.


# ---------------------------------------------------------------------------
# apply_runtime_config — guardrails
# ---------------------------------------------------------------------------


def test_apply_installs_input_guardrail_on_specific_tool() -> None:
    reg = ToolRegistry()
    reg.register(_make_tool("echo"))
    reg.register(_make_tool("other"))

    snap = RuntimeConfigSnapshot(
        guardrails=(
            GuardrailSpec(
                side="input", kind="regex_block",
                params={"pattern": r"sk-\w+", "mode": "reject"},
                tool="echo",
            ),
        )
    )
    apply_runtime_config(snap, reg)

    echo = reg.get("echo")
    other = reg.get("other")
    assert len(echo.input_guardrails) == 1
    assert isinstance(echo.input_guardrails[0], InputGuardrail)
    assert len(other.input_guardrails) == 0


def test_apply_installs_wildcard_guardrail_on_every_tool() -> None:
    reg = ToolRegistry()
    reg.register(_make_tool("a"))
    reg.register(_make_tool("b"))

    snap = RuntimeConfigSnapshot(
        guardrails=(
            GuardrailSpec(
                side="output", kind="max_length",
                params={"max_chars": 100}, tool="*",
            ),
        )
    )
    apply_runtime_config(snap, reg)

    for name in ("a", "b"):
        gs = reg.get(name).output_guardrails
        assert len(gs) == 1
        assert isinstance(gs[0], MaxLengthOutput)


def test_apply_replaces_runtime_guardrails_on_re_apply() -> None:
    """A second apply with a smaller list removes the previously installed
    runtime guardrails — admins can revert by clearing the JSON."""
    reg = ToolRegistry()
    reg.register(_make_tool("t"))

    snap1 = RuntimeConfigSnapshot(
        guardrails=(
            GuardrailSpec(
                side="output", kind="max_length",
                params={"max_chars": 50}, tool="*",
            ),
        )
    )
    apply_runtime_config(snap1, reg)
    assert len(reg.get("t").output_guardrails) == 1

    snap2 = RuntimeConfigSnapshot()  # no guardrails
    apply_runtime_config(snap2, reg)
    assert len(reg.get("t").output_guardrails) == 0


def test_apply_preserves_code_defined_guardrails() -> None:
    """Guardrails attached at registration time (not via runtime_config)
    must survive a runtime_config apply that adds its own guardrails."""
    reg = ToolRegistry()
    code_guard = MaxLengthOutput(max_chars=10, name="code-defined")
    tool = ToolDefinition(
        name="t",
        description="t",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        output_guardrails=[code_guard],
    )
    reg.register(tool)

    snap = RuntimeConfigSnapshot(
        guardrails=(
            GuardrailSpec(
                side="output", kind="regex_block",
                params={"pattern": "x", "mode": "redact"}, tool="*",
            ),
        )
    )
    apply_runtime_config(snap, reg)
    guards = reg.get("t").output_guardrails
    assert len(guards) == 2  # code-defined + runtime
    assert any(g is code_guard for g in guards)

    # Re-apply with empty snapshot — runtime guardrail removed,
    # code-defined survives.
    apply_runtime_config(RuntimeConfigSnapshot(), reg)
    guards = reg.get("t").output_guardrails
    assert len(guards) == 1
    assert guards[0] is code_guard


def test_apply_skips_guardrail_with_bad_params() -> None:
    reg = ToolRegistry()
    reg.register(_make_tool("t"))
    snap = RuntimeConfigSnapshot(
        guardrails=(
            # max_chars must be positive — passing 0 raises in the
            # guardrail's __init__; the apply layer logs and skips.
            GuardrailSpec(
                side="output", kind="max_length",
                params={"max_chars": 0}, tool="*",
            ),
        )
    )
    apply_runtime_config(snap, reg)
    assert reg.get("t").output_guardrails == []


# ---------------------------------------------------------------------------
# apply_runtime_config — workspace caps
# ---------------------------------------------------------------------------


def test_apply_returns_base_caps_when_no_workspace_overrides() -> None:
    reg = ToolRegistry()
    base = WorkspaceCaps(network=True, max_exec_seconds=10)
    out = apply_runtime_config(
        RuntimeConfigSnapshot(), reg, base_workspace_caps=base,
    )
    assert out is base


def test_apply_overlays_workspace_overrides_on_base() -> None:
    reg = ToolRegistry()
    base = WorkspaceCaps(network=True, max_exec_seconds=10, fs_write=True)
    snap = RuntimeConfigSnapshot(
        workspace=WorkspaceSpec(network=False, max_exec_seconds=60),
    )
    out = apply_runtime_config(snap, reg, base_workspace_caps=base)
    assert out.network is False  # overridden
    assert out.max_exec_seconds == 60  # overridden
    assert out.fs_write is True  # preserved from base


def test_apply_default_caps_when_no_base_supplied() -> None:
    reg = ToolRegistry()
    snap = RuntimeConfigSnapshot(
        workspace=WorkspaceSpec(exec_python=True),
    )
    out = apply_runtime_config(snap, reg)
    assert out.exec_python is True
    # Default WorkspaceCaps fields stayed at their defaults.
    assert out.network is False


# ---------------------------------------------------------------------------
# RuntimeConfigPoller
# ---------------------------------------------------------------------------


CSP_URL = "http://csp-test:8000/api/agents/me/runtime-config"


@pytest.mark.asyncio
@respx.mock
async def test_poller_first_fetch_applies_snapshot() -> None:
    cfg = {
        "tool_permissions": {"deny_tools": ["exec_bash"]},
        "guardrails": {
            "output": [{"kind": "max_length", "max_chars": 200}],
        },
    }
    respx.get(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": 1, "agent_name": "x",
                "runtime_config": cfg, "etag": "v1",
            },
        )
    )
    reg = ToolRegistry()
    reg.register(_make_tool("exec_bash"))

    poller = RuntimeConfigPoller(
        csp_base_url="http://csp-test:8000",
        csp_service_token="svc-token",
        registry=reg,
        interval_seconds=999,  # never tick during test
    )
    await poller.start()
    try:
        assert poller.last_etag == "v1"
        assert reg.get("exec_bash").permission == ToolPermission.DENY
        assert len(reg.get("exec_bash").output_guardrails) == 1
    finally:
        await poller.stop()


@pytest.mark.asyncio
@respx.mock
async def test_poller_skips_apply_on_etag_match() -> None:
    """When CSP returns the same etag, the snapshot is NOT re-parsed
    or re-applied — the poller short-circuits."""
    apply_count = 0
    poll_count = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        poll_count += 1
        return httpx.Response(
            200,
            json={
                "agent_id": 1, "agent_name": "x",
                "runtime_config": {"tool_permissions": {"ask_tools": ["t"]}},
                "etag": "stable-etag",
            },
        )

    respx.get(CSP_URL).mock(side_effect=handler)
    reg = ToolRegistry()
    reg.register(_make_tool("t"))

    async def on_change(_snap, _caps) -> None:
        nonlocal apply_count
        apply_count += 1

    poller = RuntimeConfigPoller(
        csp_base_url="http://csp-test:8000",
        csp_service_token="svc-token",
        registry=reg,
        on_change=on_change,
        interval_seconds=999,
    )
    await poller.start()
    try:
        assert apply_count == 1
        # Manually trigger a second poll — should hit the etag cache.
        await poller._poll_once()
        assert poll_count == 2
        assert apply_count == 1  # unchanged — apply skipped
    finally:
        await poller.stop()


@pytest.mark.asyncio
@respx.mock
async def test_poller_handles_5xx_without_clobbering_state() -> None:
    """A failed poll keeps the previously-applied snapshot in place."""
    # First call OK, second call 503.
    responses = [
        httpx.Response(
            200,
            json={
                "agent_id": 1, "agent_name": "x",
                "runtime_config": {"tool_permissions": {"ask_tools": ["t"]}},
                "etag": "v1",
            },
        ),
        httpx.Response(503, content=b"backend down"),
    ]
    call_idx = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        r = responses[call_idx]
        call_idx += 1
        return r

    respx.get(CSP_URL).mock(side_effect=handler)
    reg = ToolRegistry()
    reg.register(_make_tool("t"))

    poller = RuntimeConfigPoller(
        csp_base_url="http://csp-test:8000",
        csp_service_token="svc-token",
        registry=reg,
        interval_seconds=999,
    )
    await poller.start()
    try:
        assert reg.get("t").permission == ToolPermission.ASK
        # Force a second poll — gets 503 and should NOT touch state.
        await poller._poll_once()
        assert reg.get("t").permission == ToolPermission.ASK
        assert poller.last_etag == "v1"
    finally:
        await poller.stop()


@pytest.mark.asyncio
@respx.mock
async def test_poller_handles_network_error_silently() -> None:
    respx.get(CSP_URL).mock(
        side_effect=httpx.ConnectError("boom")
    )
    reg = ToolRegistry()

    poller = RuntimeConfigPoller(
        csp_base_url="http://csp-test:8000",
        csp_service_token="svc-token",
        registry=reg,
        interval_seconds=999,
    )
    # Should not raise — just log and keep going.
    await poller.start()
    try:
        assert poller.last_etag == ""
        assert poller.last_snapshot is None
    finally:
        await poller.stop()


@pytest.mark.asyncio
async def test_poller_skips_when_no_service_token() -> None:
    reg = ToolRegistry()
    poller = RuntimeConfigPoller(
        csp_base_url="http://csp-test:8000",
        csp_service_token=None,
        registry=reg,
    )
    await poller.start()  # should be a no-op
    assert poller._task is None
    await poller.stop()  # idempotent


@pytest.mark.asyncio
@respx.mock
async def test_poller_invokes_on_change_with_snapshot_and_caps() -> None:
    respx.get(CSP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "agent_id": 1, "agent_name": "x",
                "runtime_config": {
                    "workspace": {"network": True, "exec_python": True},
                },
                "etag": "v1",
            },
        )
    )
    reg = ToolRegistry()
    seen = []

    async def on_change(snap, caps) -> None:
        seen.append((snap, caps))

    poller = RuntimeConfigPoller(
        csp_base_url="http://csp-test:8000",
        csp_service_token="svc-token",
        registry=reg,
        on_change=on_change,
        interval_seconds=999,
    )
    await poller.start()
    try:
        assert len(seen) == 1
        snap, caps = seen[0]
        assert isinstance(snap, RuntimeConfigSnapshot)
        assert isinstance(caps, WorkspaceCaps)
        assert caps.network is True
        assert caps.exec_python is True
    finally:
        await poller.stop()
