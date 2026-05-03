"""Memory primitive — message history (per-run) + semantic store (cross-run).

Architecture spec ``docs/anila-agent-framework-architecture.md`` §3.3.

Two distinct concerns the framework deliberately keeps separate:

- ``MessageHistory`` — append-only conversation log within a single
  ``RunState``. Ephemeral by default; persisted as part of the
  serialised RunState when checkpointing.
- ``SemanticMemory`` — long-lived facts about the user / project /
  agent itself. Survives across runs. Typed slots (``MemoryKind``)
  drive recall filtering.

Why split: the two have very different access patterns. History is
append-only sequence access ("what did the user say two turns ago?").
Semantic memory is keyed semantic recall ("what does the user prefer
about answer length?"). Conflating them — as openai-agents-python
does with its Session-as-history Protocol — bleeds the boundary and
forces ad-hoc extraction logic.

Reference impls live in ``in_memory.py``. Real-world deployments
inject their own (AgenticRAG bridges to its memdir + relevance
selector via ``runtime/bridge/semantic_memory_bridge.py``).
"""

from agentic_rag.runtime.framework.memory.in_memory import (
    InMemoryMessageHistory,
    InMemorySemanticMemory,
)
from agentic_rag.runtime.framework.memory.protocol import (
    MemoryEntry,
    MemoryKind,
    MessageHistory,
    SemanticMemory,
)

__all__ = [
    "InMemoryMessageHistory",
    "InMemorySemanticMemory",
    "MemoryEntry",
    "MemoryKind",
    "MessageHistory",
    "SemanticMemory",
]
