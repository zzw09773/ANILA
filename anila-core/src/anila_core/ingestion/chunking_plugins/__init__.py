"""Chunking plug-in layer (docs/ingestion-platform-design.md §5).

Public surface:

- ``ChunkResult`` / ``ChunkerStrategy`` — base types from ``base``.
- ``register_chunker`` — decorator plug-ins use to enrol themselves.
- ``get_chunker`` / ``list_chunkers`` — registry lookups for the worker
  and API.

Importing this package eagerly imports ``builtins`` so the three Sprint 1
strategies (``hierarchical`` / ``fixed`` / ``markdown-aware``) are
guaranteed to be in the registry before any consumer calls
``get_chunker(...)``.
"""

from anila_core.ingestion.chunking_plugins.base import (
    ChunkerStrategy,
    ChunkResult,
)
from anila_core.ingestion.chunking_plugins.registry import (
    get_chunker,
    list_chunkers,
    register_chunker,
)

# Import builtins for their side-effect (registration). Must come after
# the imports above — builtins depends on `base` + `registry`.
from anila_core.ingestion.chunking_plugins import builtins  # noqa: F401

__all__ = [
    "ChunkResult",
    "ChunkerStrategy",
    "get_chunker",
    "list_chunkers",
    "register_chunker",
]
