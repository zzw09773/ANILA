"""anila-core consumes, but does not duplicate, the thin wire contracts."""

from pathlib import Path

from anila_contracts import (
    AgentError,
    Classification,
    InvocationCommand,
    SafeSummary,
    SourceSnapshot,
    StepEvent,
    TaskContext,
    TraceContext,
)
import anila_core.contracts as core_contracts
from anila_core.contracts import (
    AgentError as CoreAgentError,
    Classification as CoreClassification,
    InvocationCommand as CoreInvocationCommand,
    SafeSummary as CoreSafeSummary,
    SourceSnapshot as CoreSourceSnapshot,
    StepEvent as CoreStepEvent,
    TaskContext as CoreTaskContext,
    TraceContext as CoreTraceContext,
)


def test_core_contract_exports_are_identity_preserving_facades() -> None:
    assert set(core_contracts.__all__) == {
        "AgentError",
        "Classification",
        "InvocationCommand",
        "SafeSummary",
        "SourceSnapshot",
        "StepEvent",
        "TaskContext",
        "TraceContext",
    }
    assert CoreAgentError is AgentError
    assert CoreClassification is Classification
    assert CoreInvocationCommand is InvocationCommand
    assert CoreSafeSummary is SafeSummary
    assert CoreSourceSnapshot is SourceSnapshot
    assert CoreStepEvent is StepEvent
    assert CoreTaskContext is TaskContext
    assert CoreTraceContext is TraceContext


def test_core_requires_contracts_v1_without_accepting_a_future_major() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"anila-contracts>=1.0.0,<2.0.0"' in pyproject
