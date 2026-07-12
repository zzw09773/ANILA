"""anila-core consumes, but does not duplicate, the thin F5 contracts."""

from anila_contracts import AgentError, Classification, StepEvent
import anila_core.contracts as core_contracts
from anila_core.contracts import (
    AgentError as CoreAgentError,
    Classification as CoreClassification,
    StepEvent as CoreStepEvent,
)


def test_core_contract_exports_are_identity_preserving_facades() -> None:
    assert set(core_contracts.__all__) == {
        "AgentError",
        "Classification",
        "StepEvent",
    }
    assert CoreAgentError is AgentError
    assert CoreClassification is Classification
    assert CoreStepEvent is StepEvent
