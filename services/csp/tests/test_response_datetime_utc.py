# -*- coding: utf-8 -*-
"""ApiResponseModel datetime JSON round-trip — three input shapes, one instant.

Also pins the three surfaces called out in X.3 acceptance: audit log, usage
(feedback timestamps feed the governance usage / leadership view), and one
ordinary resource (collection).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas.audit_log import AuditLogResponse
from app.schemas.base import ApiResponseModel
from app.schemas.ingestion import CollectionResponse


class _Probe(ApiResponseModel):
    created_at: datetime


_INSTANT = datetime(2026, 7, 30, 8, 0, 0, 0)
_AWARE_UTC = _INSTANT.replace(tzinfo=timezone.utc)
_AWARE_TAIPEI = _AWARE_UTC.astimezone(timezone(timedelta(hours=8)))


def _assert_same_instant(iso: str) -> None:
    assert iso.endswith("Z") or "+" in iso[10:] or iso[-6] in "+-", iso
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    assert parsed.astimezone(timezone.utc) == _AWARE_UTC


def test_naive_utc_emits_explicit_offset() -> None:
    iso = _Probe(created_at=_INSTANT).model_dump(mode="json")["created_at"]
    _assert_same_instant(iso)


def test_aware_utc_emits_explicit_offset() -> None:
    iso = _Probe(created_at=_AWARE_UTC).model_dump(mode="json")["created_at"]
    _assert_same_instant(iso)


def test_aware_non_utc_emits_explicit_offset_same_instant() -> None:
    iso = _Probe(created_at=_AWARE_TAIPEI).model_dump(mode="json")["created_at"]
    _assert_same_instant(iso)


def test_audit_log_response_created_at_has_offset() -> None:
    row = AuditLogResponse(
        id=1,
        actor_user_id=1,
        actor_username="tz-probe",
        action="login",
        resource_type="session",
        resource_id=None,
        status="success",
        detail=None,
        ip_address=None,
        metadata=None,
        created_at=_INSTANT,
    )
    iso = row.model_dump(mode="json")["created_at"]
    _assert_same_instant(iso)


def test_usage_feedback_item_created_at_has_offset() -> None:
    """Feedback timestamps sit behind the governance usage / leadership view."""
    from app.api.admin.feedback import FeedbackItem

    row = FeedbackItem(
        message_id=1,
        conversation_id=1,
        rating="up",
        comment=None,
        reasons=[],
        model_name=None,
        agent_name=None,
        classification_level="無機密",
        message_created_at=_INSTANT,
        username="tz-probe",
    )
    iso = row.model_dump(mode="json")["message_created_at"]
    _assert_same_instant(iso)


def test_collection_response_created_at_has_offset() -> None:
    """Ordinary resource: CollectionResponse.created_at must carry offset."""
    row = CollectionResponse(
        id=1,
        name="tz-probe",
        description=None,
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="nvidia/NV-embed-V2",
        embedding_dim=4000,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=1,
        created_at=_INSTANT,
        updated_at=_INSTANT,
    )
    payload = row.model_dump(mode="json")
    _assert_same_instant(payload["created_at"])
    _assert_same_instant(payload["updated_at"])


def test_plain_basemodel_without_api_response_still_offsetless() -> None:
    """Revert guard: without ApiResponseModel the bug shape returns.

    Confirms the test goes red if someone drops the base class on a DTO.
    """
    from pydantic import BaseModel

    class _Bare(BaseModel):
        created_at: datetime

    iso = _Bare(created_at=_INSTANT).model_dump(mode="json")["created_at"]
    assert "Z" not in iso and "+" not in iso[10:] and iso[-6] not in "+-"
