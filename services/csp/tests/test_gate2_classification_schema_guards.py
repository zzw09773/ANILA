"""Focused Gate 2 schema/API guards for explicit classification values."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anila_contracts import Classification
from app.api import models as models_api
from app.api.agents.registration import (
    AgentRegisterRequest,
    _optional_classification_ceiling,
)
from app.models.agent import Agent
from app.models.artifact import ExportRecord
from app.models.model_registry import ModelRegistry
from app.models.registered_service import RegisteredService
from app.models.service_launch import ServiceAuditCallback
from app.schemas.contracts.artifacts import ExportRecordOut
from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.schemas.registered_service import (
    AuditCallbackPayload,
    RegisteredServiceCreate,
    RegisteredServiceUpdate,
)


def test_api_defaults_are_explicit_and_updates_preserve_omission() -> None:
    model = ModelCreate(
        name="m",
        display_name="M",
        model_type="llm",
        endpoint_url="https://model.invalid/v1",
    )
    service = RegisteredServiceCreate(name="S", entry_url="https://service.invalid")
    agent = AgentRegisterRequest(
        name="a",
        endpoint_url="https://agent.invalid/v1",
        description_for_router="test",
        base_model_id=1,
    )
    callback = AuditCallbackPayload(event_type="test.event")

    for value in (
        model.classification_ceiling,
        service.classification_ceiling,
        agent.classification_ceiling,
        callback.classification_level,
    ):
        assert value is Classification.UNCLASSIFIED

    assert "classification_ceiling" not in ModelUpdate().model_dump(
        exclude_unset=True
    )
    assert "classification_ceiling" not in RegisteredServiceUpdate().model_dump(
        exclude_unset=True
    )
    assert ExportRecordOut.model_fields[
        "target_classification_floor"
    ].is_required()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ModelCreate(
            name="m",
            display_name="M",
            model_type="llm",
            endpoint_url="https://model.invalid/v1",
            classification_ceiling=None,  # type: ignore[arg-type]
        ),
        lambda: ModelUpdate(classification_ceiling=None),  # type: ignore[arg-type]
        lambda: RegisteredServiceCreate(
            name="S",
            entry_url="https://service.invalid",
            classification_ceiling=None,  # type: ignore[arg-type]
        ),
        lambda: RegisteredServiceUpdate(
            classification_ceiling=None  # type: ignore[arg-type]
        ),
        lambda: AgentRegisterRequest(
            name="a",
            endpoint_url="https://agent.invalid/v1",
            description_for_router="test",
            base_model_id=1,
            classification_ceiling=None,  # type: ignore[arg-type]
        ),
        lambda: AuditCallbackPayload(
            event_type="test.event",
            classification_level=None,  # type: ignore[arg-type]
        ),
    ],
)
def test_explicit_null_is_rejected(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_orm_columns_are_non_null_with_unclassified_server_defaults() -> None:
    # agents.classification_ceiling is nullable again (= UI 「無上限」);
    # other Gate 2 policy ceilings stay NOT NULL.
    assert Agent.__table__.c.classification_ceiling.nullable is True
    assert Agent.__table__.c.classification_ceiling.server_default is not None
    assert "無機密" in str(
        Agent.__table__.c.classification_ceiling.server_default.arg
    )

    columns = (
        ModelRegistry.__table__.c.classification_ceiling,
        RegisteredService.__table__.c.classification_ceiling,
        ServiceAuditCallback.__table__.c.classification_level,
        ExportRecord.__table__.c.target_classification_floor,
    )
    for column in columns:
        assert column.nullable is False
        assert column.server_default is not None
        assert "無機密" in str(column.server_default.arg)


def test_serializers_fail_closed_on_corrupt_null_ceiling() -> None:
    model = ModelRegistry(
        name="bad-model",
        display_name="Bad model",
        model_type="llm",
        endpoint_url="https://model.invalid/v1",
        classification_ceiling=None,
    )
    with pytest.raises(RuntimeError, match="non-null canonical"):
        models_api._build_response(model)

    agent = Agent(
        name="bad-agent",
        owner_user_id=1,
        endpoint_url="https://agent.invalid/v1",
        description_for_router="test",
        classification_ceiling=None,
    )
    # Agent null ceiling is the legal 「無上限」wire value.
    assert _optional_classification_ceiling(agent) is None
