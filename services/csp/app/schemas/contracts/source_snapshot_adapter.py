# -*- coding: utf-8 -*-
"""Adapter from CSP's authoritative SourceSnapshot shapes to Gate 2 v1.

The database and existing CSP API keep integer identifiers and the historical
``classification_level`` name.  This adapter is the only normalization seam:
it adds the required wire version and treats SQLAlchemy/SQLite naive datetimes
as UTC.  It deliberately does not coerce IDs, classification values, hashes,
or incomplete non-``none`` snapshots; the canonical contract rejects them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from anila_contracts import SourceSnapshot as SourceSnapshotContract
from anila_contracts.sources import SOURCE_SNAPSHOT_SCHEMA_VERSION

from app.models.source_snapshot import SourceSnapshot as SourceSnapshotModel
from app.schemas.contracts.tasks import SourceSnapshotOut

SourceSnapshotShape = SourceSnapshotModel | SourceSnapshotOut


def _as_storage_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("SourceSnapshot.created_at 必須是 datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_source_snapshot_contract(snapshot: SourceSnapshotShape) -> SourceSnapshotContract:
    """Validate a CSP ORM/API snapshot as the canonical Gate 2 v1 contract."""

    return SourceSnapshotContract.model_validate(
        {
            "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
            "id": snapshot.id,
            "task_id": snapshot.task_id,
            "origin": _as_storage_value(snapshot.origin),
            "source_scope": _as_storage_value(snapshot.source_scope),
            "collection_ids": snapshot.collection_ids,
            "document_ids": snapshot.document_ids,
            "chunk_ids": snapshot.chunk_ids,
            "document_versions": snapshot.document_versions,
            "retrieval_queries": snapshot.retrieval_queries,
            "content_hash": snapshot.content_hash,
            "payload_ref": snapshot.payload_ref,
            "classification_level": _as_storage_value(snapshot.classification_level),
            "created_at": _as_utc(snapshot.created_at),
        }
    )


__all__ = ["to_source_snapshot_contract"]
