"""Chunker plug-in interface (docs/ingestion-platform-design.md §5.1).

Two public types:

- ``ChunkResult`` — single output unit; the worker writes one row per
  ``ChunkResult`` into ``document_chunks``.
- ``ChunkerStrategy`` — ABC every plug-in inherits. Subclasses must set
  the four class-level attributes (``name`` / ``display_name`` /
  ``default_params`` / ``param_schema``) so the registry / API / UI can
  discover them without instantiation.

The Protocol-vs-ABC choice is intentional: registration is by class
(``@register_chunker``), so ABC keeps inheritance explicit — a Protocol
would let a typo'd plug-in pass type checks but fail at registration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass(frozen=True)
class ChunkResult:
    """One chunk produced by a strategy.

    Frozen because the worker passes the same instance across embedding,
    storage and audit stages — accidental mutation midway would break
    deterministic re-runs (used by the evaluator to compare strategies).

    Attributes:
        content: The chunk text. Must be non-empty.
        chunk_key: Stable hierarchical id (e.g. ``"doc123/ch1/sec2/para5"``).
            Persisted into ``document_chunks.chunk_key`` and used as part of
            the ``UNIQUE (collection_id, chunk_key)`` constraint, so callers
            must ensure uniqueness within one document.
        token_count: Tokeniser-agnostic chunk size estimate (whatever the
            strategy used internally — character/word/BPE). UIs sort and
            display this; the field is advisory, not authoritative.
        metadata: Free-form auditing context (heading path, page, parent
            chunk_key, source byte range, ...). JSON-serialisable.
    """

    content: str
    chunk_key: str
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


class ChunkerStrategy(ABC):
    """Base class every chunker plug-in extends.

    Subclasses are registered via ``@register_chunker`` (see
    ``anila_core.ingestion.chunking_plugins.registry``). The four class
    attributes below are read by the registry without constructing the
    instance, so they MUST be set as class-level constants — not in
    ``__init__``.
    """

    # Stable id used in ``ingestion_collections.chunking_config["strategy"]``.
    # Treat as part of the public API: never rename, only deprecate-then-add.
    name: ClassVar[str]

    # Human-friendly label used in the dev UI strategy dropdown.
    display_name: ClassVar[str]

    # Defaults applied when the dev creates a collection without
    # over-specifying. Merged shallow-over-incoming params at chunk() time.
    default_params: ClassVar[dict[str, Any]]

    # JSON Schema (draft-2020) describing the params object so the dev UI
    # can render a form without strategy-specific frontend code. Keep the
    # schema *narrow* — every editable field becomes a UI knob.
    param_schema: ClassVar[dict[str, Any]]

    # Set to True by strategies that need embeddings to run (e.g.
    # ``semantic``). The ingestion-worker checks this flag and pre-
    # computes embeddings for the candidate segments before calling
    # ``chunk()``, passing them in via ``params["_embeddings"]`` and
    # ``params["_segments"]``. Default False keeps the contract simple
    # for the common case of pure-text-input chunkers.
    requires_embedder: ClassVar[bool] = False

    @abstractmethod
    def chunk(
        self,
        document_text: str,
        metadata: dict[str, Any],
        params: dict[str, Any],
    ) -> list[ChunkResult]:
        """Split one document into ordered ``ChunkResult``s.

        MUST be deterministic given the same (text, metadata, params)
        triple — the evaluator re-runs strategies on identical inputs to
        compute Hit@k / MRR / NDCG; non-determinism would inflate variance.
        """

    def estimate_chunks(self, document_text: str, params: dict[str, Any]) -> int:
        """Fast count estimate for the UI's pre-upload preview.

        Default uses a coarse 4-chars-per-token assumption against
        ``params["size"]`` if present, else ``params["max_leaf_tokens"]``,
        else 1024. Subclasses override only when their math differs
        materially (e.g. ``pdf-page`` returns the page count exactly).
        """
        target = (
            params.get("size")
            or params.get("max_leaf_tokens")
            or params.get("target_tokens")
            or 1024
        )
        return max(1, len(document_text) // (target * 4))
