"""Gate 5 R4 pure StreamBridge/store/AgentClient conformance tests.

The in-memory store in this file is explicitly a conformance double.  These
tests do not claim that CSP has a durable DB adapter yet.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from anila_contracts import Classification, ExecutionGrant, StepEvent
from anila_contracts._types import InvocationTargetKind
from anila_contracts.agents import ModelBinding
from anila_contracts.grants import ExecutionTarget
from anila_contracts.contexts import AuthAssurance
from anila_contracts.events import StepKind, StepStatus
from app.services.proxy.stream_bridge import (
    AgentCallBinding,
    CspProxyAgentClient,
    CspProxyRequiredError,
    EventBindingError,
    EventBudgetExceeded,
    EventConflictError,
    EventOrderError,
    InMemorySessionEventStore,
    StreamBridge,
    TerminalConflictError,
    TerminalEventError,
    BridgeContext,
)


def _context(run_id: str = "run-1") -> BridgeContext:
    return BridgeContext(
        task_id="task-1",
        trace_id="trace-1",
        agent_id="agent-1",
        session_id="session-1",
        run_id=run_id,
        classification=Classification.CONFIDENTIAL,
        source_snapshot_id=1,
    )


def _grant(*, expires_delta: timedelta = timedelta(minutes=1)) -> ExecutionGrant:
    now = datetime.now(timezone.utc)
    return ExecutionGrant(
        schema_version="execution-grant/v1",
        grant_id="grant-1",
        task_id=1,
        run_id=1,
        trace_id="trace-1",
        invocation_id="invoke-1",
        source_snapshot_id=1,
        route_decision_id="route-1",
        policy_decision_id="pg-route-1",
        registry_snapshot_id="snap-1",
        classification=Classification.CONFIDENTIAL,
        auth_assurance=AuthAssurance(
            sid="sid-1",
            amr=("pwd",),
            acr="aal2",
            auth_time=now - timedelta(minutes=1),
            break_glass=False,
        ),
        target=ExecutionTarget(
            kind=InvocationTargetKind.AGENT,
            id="agent-1",
            model_binding=ModelBinding(model_id=1),
        ),
        manifest_revision="sha256:rev-1",
        allowed_capabilities=("streaming",),
        allowed_scopes=("agent:invoke",),
        issued_at=now - timedelta(seconds=30),
        expires_at=now + expires_delta,
        session_id="session-1",
    )


def _event(*, event_id: str = "evt-1", cursor: str = "1", sequence: int = 1, **updates: object) -> StepEvent:
    value = StepEvent(
        event_id=event_id,
        sequence=sequence,
        cursor=cursor,
        trace_id="forged-trace",
        task_id="forged-task",
        session_id="forged-session",
        invocation_id="inv-1",
        run_id="forged-run",
        step_id="tool:1",
        kind=StepKind.TOOL,
        status=StepStatus.RUNNING,
        agent_id="forged-agent",
        tool_name="search_documents",
        safe_input_summary="開始文件搜尋",
        classification=Classification.UNCLASSIFIED,
    ).model_dump(mode="json")
    value.update(updates)
    return StepEvent.model_validate(value)


def _payload(frame: str) -> dict[str, object]:
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


def test_append_rebinds_forged_ids_and_assigns_server_cursor():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)

    frame = bridge.append("anila.step", _event().model_dump_json())

    assert isinstance(frame, str)
    payload = _payload(frame)
    assert payload["task_id"] == "task-1"
    assert payload["trace_id"] == "trace-1"
    assert payload["agent_id"] == "agent-1"
    assert payload["session_id"] == "session-1"
    assert payload["run_id"] == "run-1"
    assert payload["classification"] == "機密"
    assert payload["cursor"] == "1"


def test_duplicate_replay_returns_existing_event_and_conflicting_payload_is_409():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)
    first = bridge.append_event(_event())
    assert first is not None and first.duplicate is False

    duplicate = bridge.append_event(_event())
    assert duplicate is not None and duplicate.duplicate is True
    assert duplicate.event == first.event

    with pytest.raises(EventConflictError) as exc_info:
        bridge.append_event(_event(safe_output_summary="different"))
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "EVENT_ID_CONFLICT"


def test_out_of_order_new_event_fails_closed_but_duplicate_is_allowed():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)
    bridge.append_event(_event(event_id="evt-1", cursor="2", sequence=2))

    with pytest.raises(EventOrderError):
        bridge.append_event(_event(event_id="evt-2", cursor="1", sequence=1))
    assert bridge.replay_events() and len(bridge.replay_events()) == 1
    assert bridge.append_event(_event(event_id="evt-1", cursor="2", sequence=2)).duplicate is True  # type: ignore[union-attr]


def test_source_order_uses_sequence_not_forged_numeric_or_non_numeric_cursor():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)
    first = bridge.append_event(_event(cursor="9" * 1000, sequence=1))
    second = bridge.append_event(_event(event_id="evt-2", cursor="not-a-cursor", sequence=2))

    assert first is not None and second is not None
    assert [event.cursor for event in bridge.replay_events()] == ["1", "2"]


def test_replay_after_cursor_survives_new_bridge_instance_with_shared_store():
    store = InMemorySessionEventStore()
    first_bridge = StreamBridge(_context(), store=store)
    first_bridge.append_event(_event(event_id="evt-1", cursor="1", sequence=1))
    first_bridge.append_event(_event(event_id="evt-2", cursor="2", sequence=2))

    restarted_bridge = StreamBridge(_context(), store=store)
    replay = restarted_bridge.replay_events(after_cursor=1)

    assert [event.event_id for event in replay] == ["evt-2"]
    assert [event.cursor for event in restarted_bridge.replay_events(after_cursor=0)] == ["1", "2"]


def test_wrong_trusted_binding_cannot_read_or_append_existing_run():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)
    bridge.append_event(_event())
    wrong = StreamBridge(
        BridgeContext(
            task_id="other-task",
            trace_id="trace-1",
            agent_id="agent-1",
            session_id="session-1",
            run_id="run-1",
            classification=Classification.CONFIDENTIAL,
        ),
        store=store,
    )

    with pytest.raises(EventBindingError):
        wrong.replay_events()


def test_terminal_is_exactly_once_and_blocks_later_events():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)
    terminal = bridge.append_terminal(StepStatus.CANCELLED, safe_output_summary="已取消")
    duplicate = bridge.append_terminal(StepStatus.CANCELLED, safe_output_summary="已取消")

    assert terminal.event == duplicate.event
    assert duplicate.duplicate is True
    assert bridge.terminal_event() == terminal.event
    with pytest.raises(TerminalEventError):
        bridge.append_event(_event(event_id="after-terminal", cursor="2", sequence=2))


def test_completed_agent_wire_events_do_not_latch_until_csp_authority():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)

    # Tool completion and TimelineRunHooks.on_agent_end both use the ordinary
    # Agent wire path.  Neither is CSP authority to close the run.
    tool_completed = bridge.append_event(
        _event(event_id="tool-completed", sequence=1, status=StepStatus.COMPLETED)
    )
    agent_completed = bridge.append_event(
        _event(
            event_id="agent-completed",
            sequence=2,
            status=StepStatus.COMPLETED,
            kind=StepKind.AGENT,
            step_id="agent:root",
            tool_name=None,
        )
    )
    after_agent = bridge.append_event(
        _event(event_id="after-agent", sequence=3, status=StepStatus.RUNNING)
    )

    assert tool_completed is not None
    assert agent_completed is not None
    assert after_agent is not None
    assert bridge.terminal_event() is None

    terminal = bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="csp-terminal",
        safe_output_summary="完成",
    )
    assert bridge.terminal_event() == terminal.event
    assert [event.event_id for event in bridge.replay_events()] == [
        "tool-completed",
        "agent-completed",
        "after-agent",
        "csp-terminal",
    ]


def test_terminal_authority_bit_mismatch_is_a_typed_conflict():
    store = InMemorySessionEventStore()
    context = _context()
    bridge = StreamBridge(context, store=store)
    first = bridge.append_event(_event(status=StepStatus.COMPLETED))
    assert first is not None

    with pytest.raises(EventConflictError) as exc_info:
        store.append(first.event, binding=context, authoritative_terminal=True)
    assert exc_info.value.code == "EVENT_ID_CONFLICT"

    terminal = bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="terminal-authority",
        safe_output_summary="完成",
    )
    with pytest.raises(EventConflictError) as reverse_exc_info:
        store.append(terminal.event, binding=context, authoritative_terminal=False)
    assert reverse_exc_info.value.code == "EVENT_ID_CONFLICT"


def test_authoritative_terminal_rejects_non_terminal_status():
    store = InMemorySessionEventStore()
    with pytest.raises(ValueError, match="authoritative terminal status"):
        store.append(
            _event(status=StepStatus.RUNNING),
            binding=_context(),
            authoritative_terminal=True,
        )


def test_terminal_retry_ignores_authority_timestamp_but_rejects_changed_payload():
    store = InMemorySessionEventStore()
    bridge = StreamBridge(_context(), store=store)
    completed_at = datetime.now(timezone.utc)
    first = bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="terminal-1",
        safe_output_summary="完成",
        completed_at=completed_at,
    )
    retry = bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="terminal-1",
        safe_output_summary="完成",
    )
    assert retry.duplicate is True and retry.event == first.event
    with pytest.raises(TerminalConflictError) as exc_info:
        bridge.append_terminal(StepStatus.FAILED, event_id="terminal-1", safe_output_summary="完成")
    assert getattr(exc_info.value, "status_code", None) == 409
    with pytest.raises(TerminalConflictError) as summary_exc:
        bridge.append_terminal(StepStatus.COMPLETED, event_id="terminal-1", safe_output_summary="不同")
    assert getattr(summary_exc.value, "status_code", None) == 409


def test_unknown_malformed_secret_and_flood_are_dropped_without_store_append():
    store = InMemorySessionEventStore()
    invalid_bridge = StreamBridge(_context(), store=store)
    assert invalid_bridge.append("anila.reasoning", '{"delta":"raw"}') is None
    assert invalid_bridge.append("anila.step", "not-json") is None
    assert invalid_bridge.append(
        "anila.step", _event(safe_output_summary="api_key=sk-abcdefghijklmnopqrst").model_dump_json()
    ) is None
    flood_bridge = StreamBridge(
        _context(), store=store, max_events_per_second=40, max_events_per_run=2
    )
    assert flood_bridge.append("anila.step", _event(event_id="ok-1").model_dump_json()) is not None
    assert flood_bridge.append("anila.step", _event(event_id="ok-2", cursor="2", sequence=2).model_dump_json()) is not None
    assert flood_bridge.append("anila.step", _event(event_id="flood", cursor="3", sequence=3).model_dump_json()) is None
    assert [event.event_id for event in store.all_events(binding=_context())] == ["ok-1", "ok-2"]


def test_durable_store_budget_survives_bridge_restart_and_terminal_bypasses_cap():
    store = InMemorySessionEventStore(max_events_per_run=1)
    first_bridge = StreamBridge(_context(), store=store, max_events_per_run=1)
    first = first_bridge.append_event(_event(event_id="budget-1", sequence=1))
    assert first is not None

    restarted_bridge = StreamBridge(_context(), store=store, max_events_per_run=1)
    with pytest.raises(EventBudgetExceeded) as exc_info:
        restarted_bridge.append_event(_event(event_id="budget-2", sequence=2))
    assert exc_info.value.code == "EVENT_RUN_BUDGET_EXCEEDED"

    terminal = restarted_bridge.append_terminal(
        StepStatus.COMPLETED,
        event_id="budget-terminal",
        safe_output_summary="完成",
    )
    assert terminal.event.cursor == "2"
    assert [event.event_id for event in restarted_bridge.replay_events()] == [
        "budget-1",
        "budget-terminal",
    ]


def test_inmemory_initial_dispatch_lease_reclaims_and_rejects_active_or_other_key():
    store = InMemorySessionEventStore()
    context = _context("dispatch-lease")
    assert store.claim_dispatch(
        binding=context, idempotency_key="dispatch-key", lease_seconds=180
    ) is True
    state = store._runs[context.run_id]
    state.dispatch_lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert store.claim_dispatch(
        binding=context, idempotency_key="dispatch-key", lease_seconds=180
    ) is False
    with pytest.raises(EventConflictError):
        store.claim_dispatch(
            binding=context, idempotency_key="different-key", lease_seconds=180
        )
    first_generation = state.dispatch_lease_generation
    state.dispatch_lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert store.claim_dispatch(
        binding=context, idempotency_key="dispatch-key", lease_seconds=180
    ) is True
    assert state.dispatch_lease_generation == first_generation + 1


def test_agent_client_only_prepares_csp_proxy_request_with_all_bindings():
    calls: list[object] = []
    client = CspProxyAgentClient(
        "https://csp.internal",
        trusted_csp_origin="https://csp.internal",
        transport=calls.append,
    )
    grant = _grant()
    binding = AgentCallBinding(
        task_id="1",
        run_id="1",
        session_id="session-1",
        trace_id="trace-1",
        agent_id="agent-1",
        grant=grant,
        snapshot_id="snap-1",
        snapshot_revision="snap-1",
        snapshot_hash="snap-1",
        revision="sha256:rev-1",
        manifest_hash="hash-1",
        source_snapshot_id=1,
        classification=Classification.CONFIDENTIAL,
    )

    result = client.dispatch({"query": "hello"}, binding=binding)

    assert result is None
    assert len(calls) == 1
    request = calls[0]
    assert request.url == "https://csp.internal/v1/chat/completions"  # type: ignore[union-attr]
    assert request.headers["X-ANILA-Registry-Snapshot-Id"] == "snap-1"  # type: ignore[union-attr]
    assert request.headers["X-ANILA-Registry-Snapshot-Revision"] == "snap-1"  # type: ignore[union-attr]
    assert request.headers["X-ANILA-Registry-Snapshot-Hash"] == "snap-1"  # type: ignore[union-attr]
    assert request.headers["X-ANILA-Manifest-Revision"] == "sha256:rev-1"  # type: ignore[union-attr]
    assert request.headers["X-ANILA-Manifest-SHA256"] == "hash-1"  # type: ignore[union-attr]
    assert request.headers["X-ANILA-Execution-Grant-Id"] == "grant-1"  # type: ignore[union-attr]
    assert request.json["model"] == "agent-1"  # type: ignore[union-attr]
    assert request.json["stream"] is True  # type: ignore[union-attr]


def test_agent_client_rejects_raw_agent_endpoint_and_endpoint_payload():
    client = CspProxyAgentClient(
        "https://csp.internal", trusted_csp_origin="https://csp.internal"
    )
    grant = _grant()
    binding = AgentCallBinding(
        task_id="1",
        run_id="1",
        session_id="session-1",
        trace_id="trace-1",
        agent_id="agent-1",
        grant=grant,
        registry_snapshot_id="snap-1",
        registry_snapshot_revision="snap-1",
        registry_snapshot_hash="snap-1",
        manifest_revision="sha256:rev-1",
        manifest_sha256="hash-1",
        source_snapshot_id=1,
        classification=Classification.CONFIDENTIAL,
    )
    with pytest.raises(CspProxyRequiredError):
        client.dispatch({}, binding=binding, raw_agent_endpoint="https://agent.internal/run")
    with pytest.raises(CspProxyRequiredError):
        client.dispatch({"endpoint_url": "https://agent.internal/run"}, binding=binding)


def test_agent_client_rejects_untrusted_origin_or_expired_grant_before_transport():
    calls: list[object] = []
    with pytest.raises(CspProxyRequiredError):
        CspProxyAgentClient(
            "https://agent.internal",
            trusted_csp_origin="https://csp.internal",
        )
    client = CspProxyAgentClient(
        "https://csp.internal",
        trusted_csp_origin="https://csp.internal",
        transport=calls.append,
    )
    grant = _grant()
    binding = AgentCallBinding(
        task_id="1",
        run_id="1",
        session_id="session-1",
        trace_id="trace-1",
        agent_id="agent-1",
        grant=grant,
        registry_snapshot_id="snap-1",
        registry_snapshot_revision="snap-1",
        registry_snapshot_hash="snap-1",
        manifest_revision="sha256:rev-1",
        manifest_sha256="hash-1",
        source_snapshot_id=1,
        classification=Classification.CONFIDENTIAL,
    )
    with pytest.raises(ValueError):
        client.dispatch({}, binding=binding, now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert calls == []
