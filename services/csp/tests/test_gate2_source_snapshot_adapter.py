from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from anila_contracts import Classification, SourceSnapshot as SourceSnapshotContract

from app.models.source_snapshot import SourceSnapshot as SourceSnapshotModel
from app.schemas.contracts.source_snapshot_adapter import to_source_snapshot_contract
from app.schemas.contracts.tasks import SnapshotOrigin, SourceScope, SourceSnapshotOut


def _snapshot_model(**overrides: object) -> SourceSnapshotModel:
    fields: dict[str, object] = {
        "id": 7,
        "task_id": 3,
        "origin": "collection",
        "source_scope": "project",
        "collection_ids": [11],
        "document_ids": [101, 102],
        "chunk_ids": ["chunk-101", "chunk-102"],
        "document_versions": {"101": "generation-4", "102": "generation-9"},
        "retrieval_queries": ["治理帳"],
        "content_hash": "a" * 64,
        "payload_ref": "snapshots/7.json",
        "classification_level": "機密",
        "created_at": datetime(2026, 7, 12, 1, 2, 3, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return SourceSnapshotModel(**fields)


def test_adapter_converts_a_complete_orm_snapshot_without_semantic_coercion() -> None:
    contract = to_source_snapshot_contract(_snapshot_model())

    assert isinstance(contract, SourceSnapshotContract)
    assert contract.id == 7
    assert contract.task_id == 3
    assert contract.document_ids == (101, 102)
    assert contract.document_versions == {
        "101": "generation-4",
        "102": "generation-9",
    }
    assert contract.classification_level is Classification.CONFIDENTIAL
    assert contract.created_at.tzinfo is timezone.utc


def test_adapter_accepts_explicit_no_source_and_treats_naive_database_time_as_utc() -> None:
    row = _snapshot_model(
        id=8,
        origin="none",
        source_scope="none",
        collection_ids=[],
        document_ids=[],
        chunk_ids=[],
        document_versions=None,
        retrieval_queries=[],
        content_hash=None,
        payload_ref=None,
        classification_level="無機密",
        created_at=datetime(2026, 7, 12, 3, 4, 5),
    )

    contract = to_source_snapshot_contract(row)

    assert contract.origin.value == "none"
    assert contract.document_versions is None
    assert contract.content_hash is None
    assert contract.created_at == datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)

    row.content_hash = "c" * 64
    with pytest.raises(ValidationError, match="content_hash"):
        to_source_snapshot_contract(row)


@pytest.mark.parametrize(
    "patch",
    [
        {"document_versions": None},
        {"document_versions": {"101": "generation-4"}},
        {"content_hash": None},
        {"content_hash": "A" * 64},
    ],
)
def test_adapter_rejects_incomplete_or_unsealed_non_none_snapshot(
    patch: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        to_source_snapshot_contract(_snapshot_model(**patch))


def test_adapter_rejects_unknown_persisted_classification() -> None:
    with pytest.raises(ValidationError, match="classification_level"):
        to_source_snapshot_contract(_snapshot_model(classification_level="公開"))


def test_adapter_accepts_existing_source_snapshot_out_shape() -> None:
    existing_wire = SourceSnapshotOut(
        id=9,
        task_id=4,
        origin=SnapshotOrigin.DOCUMENT,
        source_scope=SourceScope.ORGANIZATION,
        collection_ids=[],
        document_ids=[301],
        chunk_ids=["chunk-301"],
        document_versions={"301": "generation-2"},
        retrieval_queries=["來源查證"],
        content_hash="b" * 64,
        payload_ref="snapshots/9.json",
        classification_level=Classification.SECRET,
        created_at=datetime(2026, 7, 12, 5, 6, 7, tzinfo=timezone.utc),
    )

    contract = to_source_snapshot_contract(existing_wire)

    assert contract.id == existing_wire.id
    assert contract.task_id == existing_wire.task_id
    assert contract.document_ids == (301,)
    assert contract.classification_level is Classification.SECRET
