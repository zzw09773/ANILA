"""Gate 1 F5: CSP consumes the standalone contracts without type drift."""

from anila_contracts import AgentError, Classification, StepEvent

from app.schemas.contracts import (
    AgentError as CspAgentError,
    ClassificationLevel as CspClassificationLevel,
    StepEvent as CspStepEvent,
)
from app.schemas.contracts.classification import (
    ClassificationLevel as LegacyClassificationLevel,
)


def test_csp_exports_are_identity_preserving_contract_facades() -> None:
    assert CspClassificationLevel is Classification
    assert LegacyClassificationLevel is Classification
    assert CspStepEvent is StepEvent
    assert CspAgentError is AgentError
