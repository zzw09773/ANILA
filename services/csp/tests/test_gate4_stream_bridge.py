from __future__ import annotations

import json

from anila_contracts import Classification, StepEvent
from anila_contracts.events import StepKind, StepStatus

from app.services.proxy.stream_bridge import BridgeContext, StreamValidator


def _context() -> BridgeContext:
    return BridgeContext(
        task_id="42",
        trace_id="trusted-trace",
        agent_id="7",
        session_id="trusted-session",
        run_id="99",
        classification=Classification.CONFIDENTIAL,
    )


def _event(**updates) -> dict:
    base = StepEvent(
        event_id="evt-1",
        sequence=1,
        cursor="1",
        trace_id="forged-trace",
        task_id="forged-task",
        session_id="forged-session",
        invocation_id="inv-1",
        run_id="forged-run",
        step_id="step-1",
        kind=StepKind.TOOL,
        status=StepStatus.RUNNING,
        agent_id="forged-agent",
        tool_name="search_documents",
        safe_input_summary="執行唯讀文件搜尋",
        classification=Classification.UNCLASSIFIED,
    ).model_dump(mode="json")
    base.update(updates)
    return base


def _payload(block: str) -> dict:
    return json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: ")))


def test_rebinds_all_governance_identity_from_trusted_dispatch_context():
    block = StreamValidator(_context()).validate("anila.step", json.dumps(_event()))
    assert block is not None
    payload = _payload(block)
    assert payload["task_id"] == "42"
    assert payload["trace_id"] == "trusted-trace"
    assert payload["agent_id"] == "7"
    assert payload["session_id"] == "trusted-session"
    assert payload["run_id"] == "99"
    assert payload["classification"] == "機密"


def test_drops_raw_reasoning_unknown_malformed_and_oversize(caplog):
    validator = StreamValidator(_context(), max_event_bytes=128)
    assert validator.validate("anila.reasoning", '{"delta":"hidden"}') is None
    assert validator.validate("anila.future", "{}") is None
    assert validator.validate("anila.step", "not-json") is None
    assert validator.validate("anila.step", "x" * 129) is None
    assert "hidden" not in caplog.text


def test_drops_secret_and_large_safe_summaries_without_logging_payload(caplog):
    secret = "sk-abcdefghijklmnopqrstuv"
    validator = StreamValidator(_context())
    assert validator.validate(
        "anila.step", json.dumps(_event(safe_output_summary=f"token={secret}"))
    ) is None
    assert validator.validate(
        "anila.step", json.dumps(_event(safe_output_summary="x" * 501))
    ) is None
    assert secret not in caplog.text


def test_enforces_per_run_and_rate_budgets():
    now = [10.0]
    validator = StreamValidator(
        _context(), max_events_per_second=2, max_events_per_run=3, clock=lambda: now[0]
    )
    assert validator.validate("anila.step", json.dumps(_event(event_id="1")))
    assert validator.validate("anila.step", json.dumps(_event(event_id="2")))
    assert validator.validate("anila.step", json.dumps(_event(event_id="3"))) is None
    now[0] += 1.0
    assert validator.validate("anila.step", json.dumps(_event(event_id="4")))
    assert validator.validate("anila.step", json.dumps(_event(event_id="5"))) is None
