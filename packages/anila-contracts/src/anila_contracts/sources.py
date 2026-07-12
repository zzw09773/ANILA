"""Immutable source snapshot wire contract."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, ValidationInfo, field_validator, model_validator

from ._base import (
    ContractModel,
    Identifier,
    NonEmptyString,
    PositiveInt,
    Sha256Hex,
    ensure_unique,
)
from ._types import SnapshotOrigin, SourceScope
from .classification import ClassificationLevel

SOURCE_SNAPSHOT_SCHEMA_VERSION = "source-snapshot/v1"
PayloadReference = Annotated[str, Field(min_length=1, max_length=1000)]


class SourceSnapshot(ContractModel):
    """Immutable retrieval/source evidence bound to a Task.

    ``document_versions`` must cover exactly the declared document IDs.  This
    keeps a valid-looking but incomplete version map from weakening citation
    stability.
    """

    schema_version: Literal["source-snapshot/v1"]
    id: PositiveInt
    task_id: PositiveInt
    origin: SnapshotOrigin
    source_scope: SourceScope
    collection_ids: tuple[PositiveInt, ...] = ()
    document_ids: tuple[PositiveInt, ...] = ()
    chunk_ids: tuple[Identifier, ...] = ()
    document_versions: dict[Identifier, NonEmptyString] | None = None
    retrieval_queries: tuple[NonEmptyString, ...] = ()
    content_hash: Sha256Hex | None = None
    payload_ref: PayloadReference | None = None
    classification_level: ClassificationLevel
    created_at: AwareDatetime

    @field_validator("collection_ids", "document_ids")
    @classmethod
    def _integer_source_lists_are_unique(
        cls, value: tuple[int, ...], info: ValidationInfo
    ) -> tuple[int, ...]:
        ensure_unique(value, field_name=info.field_name or "source_list")
        return value

    @field_validator("chunk_ids", "retrieval_queries")
    @classmethod
    def _string_source_lists_are_unique(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        ensure_unique(value, field_name=info.field_name or "source_list")
        return value

    @model_validator(mode="after")
    def _source_identity_is_complete(self) -> SourceSnapshot:
        if self.origin is SnapshotOrigin.NONE:
            if self.source_scope is not SourceScope.NONE:
                raise ValueError("origin=none 必須搭配 source_scope=none")
            if (
                self.collection_ids
                or self.document_ids
                or self.chunk_ids
                or self.retrieval_queries
                or self.payload_ref is not None
            ):
                raise ValueError("origin=none 不得夾帶任何來源識別或 retrieval query")
            if self.document_versions:
                raise ValueError("origin=none 的 document_versions 只能是 null 或空 map")
            if self.content_hash is not None:
                raise ValueError("origin=none 不得夾帶 content_hash 來源證據")
            return self

        if self.content_hash is None:
            raise ValueError("非 none snapshot 必須以小寫 SHA-256 content_hash 封存")
        if self.document_versions is None:
            raise ValueError("非 none snapshot 必須包含完整 document_versions")
        expected_document_keys = {str(document_id) for document_id in self.document_ids}
        if set(self.document_versions) != expected_document_keys:
            raise ValueError(
                "document_versions 必須且只能覆蓋全部 document_ids（key=str(document_id)）"
            )

        if self.source_scope is SourceScope.NONE:
            raise ValueError("非 none 的 origin 不得使用 source_scope=none")
        if self.origin is SnapshotOrigin.SERVICE:
            if self.source_scope is not SourceScope.REGISTERED_SERVICE:
                raise ValueError("origin=service 必須搭配 registered_service scope")
        elif self.source_scope is SourceScope.REGISTERED_SERVICE:
            raise ValueError("registered_service scope 必須搭配 origin=service")

        if self.origin is SnapshotOrigin.COLLECTION and not self.collection_ids:
            raise ValueError("origin=collection 必須至少包含一個 collection_id")
        if self.origin is SnapshotOrigin.DOCUMENT and not self.document_ids:
            raise ValueError("origin=document 必須至少包含一個 document_id")
        if not (
            self.collection_ids
            or self.document_ids
            or self.chunk_ids
            or self.payload_ref is not None
        ):
            raise ValueError("非 none snapshot 必須包含可驗證的來源識別")
        return self


__all__ = ["SOURCE_SNAPSHOT_SCHEMA_VERSION", "SourceSnapshot"]
