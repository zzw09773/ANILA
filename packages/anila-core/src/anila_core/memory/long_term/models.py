"""Pure DTOs for the user-tenant memory layer.

These mirror the shapes the CSP storage backend serialises to/from
SQL, but carry no ORM dependency — anila-core is the single source
of truth for what a "fact" or "retrieved chunk" looks like, and
storage backends conform.

All three types are frozen dataclasses so an adapter can return a
list and the consumer can hash / dedupe / pass through middleware
without worrying about identity mutation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from anila_contracts import Classification


@dataclass(frozen=True)
class UserFactDTO:
    """A single piece of long-term, structured knowledge about a user.

    ``id`` is optional so the same dataclass can represent both
    persisted facts (server-assigned id) and not-yet-saved facts
    being passed into an upsert. ``confidence`` is clamped to
    ``[0.0, 1.0]`` at extraction time; consumers can trust the
    range without re-validating.
    """

    user_id: int
    key: str
    value: str
    classification_level: Classification
    classification_source: str
    confidence: float = 1.0
    id: Optional[int] = None
    source_conversation_id: Optional[int] = None
    source_message_id: Optional[int] = None
    source_task_id: Optional[int] = None
    source_snapshot_id: Optional[int] = None
    required_compartment_ids: frozenset[int] = field(default_factory=frozenset)
    source_collection_ids: frozenset[int] = field(default_factory=frozenset)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        _validate_governance(
            classification_level=self.classification_level,
            classification_source=self.classification_source,
            required_compartment_ids=self.required_compartment_ids,
            source_collection_ids=self.source_collection_ids,
        )


@dataclass(frozen=True)
class RetrievedChunk:
    """One hit from cross-conversation RAG search.

    Chunks come back ranked by ``cosine`` similarity (higher = more
    relevant). ``is_encrypted`` flows verbatim from the stored row;
    consumers are responsible for honouring it (the CSP proxy uses
    it to latch the consuming conversation into classified state —
    see P3 / migration 0031).
    """

    id: int
    conversation_id: int
    role: str  # 'user' | 'assistant'
    content: str
    cosine: float
    is_encrypted: bool
    classification_level: Classification
    classification_source: str
    source_task_id: Optional[int] = None
    source_snapshot_id: Optional[int] = None
    required_compartment_ids: frozenset[int] = field(default_factory=frozenset)
    source_collection_ids: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _validate_governance(
            classification_level=self.classification_level,
            classification_source=self.classification_source,
            required_compartment_ids=self.required_compartment_ids,
            source_collection_ids=self.source_collection_ids,
        )


@dataclass(frozen=True)
class MemoryReadResult:
    """Bundle of everything pulled before a chat completion.

    The proxy P3 latch keys off ``encryption_inherited`` — if any
    retrieved chunk was written from an encrypted conversation, the
    consuming conversation must be upgraded to classified before
    the LLM call returns. Keep the property here (rather than on the
    proxy side) so any downstream consumer of MemoryReadResult gets
    the same Bell-LaPadula semantics for free.
    """

    block: Optional[str]
    facts_count: int
    chunks: list[RetrievedChunk] = field(default_factory=list)
    inherited_classification: Optional[Classification] = None
    required_compartment_ids: frozenset[int] = field(default_factory=frozenset)
    source_collection_ids: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.facts_count < 0:
            raise ValueError("facts_count 不得為負數")
        if self.block is not None and self.inherited_classification is None:
            raise ValueError("memory block 必須帶 inherited_classification")
        if self.inherited_classification is not None and not isinstance(
            self.inherited_classification, Classification
        ):
            raise TypeError("inherited_classification 必須是 canonical Classification")
        chunk_levels = [chunk.classification_level for chunk in self.chunks]
        if chunk_levels and (
            self.inherited_classification is None
            or self.inherited_classification < Classification.max_of(chunk_levels)
        ):
            raise ValueError("inherited_classification 不得低於 recalled chunk")
        _validate_ids(self.required_compartment_ids, field_name="required_compartment_ids")
        _validate_ids(self.source_collection_ids, field_name="source_collection_ids")

    @property
    def encryption_inherited(self) -> bool:
        return bool(
            self.inherited_classification is not None
            and self.inherited_classification >= Classification.CONFIDENTIAL
        )


def _validate_ids(values: frozenset[int], *, field_name: str) -> None:
    if not isinstance(values, frozenset) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        raise ValueError(f"{field_name} 必須是正整數 frozenset")


def _validate_governance(
    *,
    classification_level: Classification,
    classification_source: str,
    required_compartment_ids: frozenset[int],
    source_collection_ids: frozenset[int],
) -> None:
    if not isinstance(classification_level, Classification):
        raise TypeError("classification_level 必須是 canonical Classification")
    if not isinstance(classification_source, str) or not classification_source.strip():
        raise ValueError("classification_source 必填")
    _validate_ids(required_compartment_ids, field_name="required_compartment_ids")
    _validate_ids(source_collection_ids, field_name="source_collection_ids")
