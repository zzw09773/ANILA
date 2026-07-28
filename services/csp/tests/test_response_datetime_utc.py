# -*- coding: utf-8 -*-
"""ApiResponseModel datetime JSON round-trip — three input shapes, one instant."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas.base import ApiResponseModel


class _Probe(ApiResponseModel):
    created_at: datetime


_INSTANT = datetime(2026, 7, 27, 11, 19, 42, 679819)
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


def test_collection_response_created_at_has_offset() -> None:
    """The reported bug shape: CollectionResponse.created_at must carry offset."""
    from anila_contracts import Classification

    from app.schemas.ingestion import CollectionResponse

    level = next(iter(Classification))
    row = CollectionResponse(
        id=1,
        name="tz-probe",
        description=None,
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="nvidia/NV-embed-V2",
        embedding_fingerprint="sha256:" + ("a" * 64),
        embedding_dim=4000,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=1,
        classification_level=level,
        created_at=_INSTANT,
        updated_at=_INSTANT,
    )
    payload = row.model_dump(mode="json")
    _assert_same_instant(payload["created_at"])
    _assert_same_instant(payload["updated_at"])
