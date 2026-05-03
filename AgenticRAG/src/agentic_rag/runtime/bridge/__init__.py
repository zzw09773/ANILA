"""Bridge layer — adapt the framework primitives to AgenticRAG's existing
shapes (stream-based ``Provider``, dict-output ``ToolDefinition``, SSE
``ServerEvent``).

Modules here are the only place where framework code (``runtime.framework.*``)
meets AgenticRAG-specific code (engine / api / providers / storage). Keeping
the bridge concentrated lets the framework directory stay generic enough that
a Phase-2 anila-core fork could re-vendor it cleanly later if needed.
"""

from agentic_rag.runtime.bridge.agent_builder import build_rag_agent
from agentic_rag.runtime.bridge.citation_guardrail import (
    CitationMissing,
    CitationReferences,
    CitationVerdict,
    check_final_answer,
    collect_references,
    enforce_citations,
)
from agentic_rag.runtime.bridge.coordinator_bridge import (
    agent_def_to_framework_agent,
    build_framework_coordinator,
    is_parallel_safe,
)
from agentic_rag.runtime.bridge.provider_adapter import FrameworkProviderAdapter
from agentic_rag.runtime.bridge.rag_actions import (
    build_keyword_search_action,
    build_read_document_action,
    build_vector_search_action,
    wrap_tool_definition,
)
from agentic_rag.runtime.bridge.semantic_memory_bridge import MemdirSemanticMemory
from agentic_rag.runtime.bridge.sse_runner import run_agentic_chat_sse

__all__ = [
    "CitationMissing",
    "CitationReferences",
    "CitationVerdict",
    "FrameworkProviderAdapter",
    "MemdirSemanticMemory",
    "agent_def_to_framework_agent",
    "build_framework_coordinator",
    "build_keyword_search_action",
    "build_rag_agent",
    "build_read_document_action",
    "build_vector_search_action",
    "check_final_answer",
    "collect_references",
    "enforce_citations",
    "is_parallel_safe",
    "run_agentic_chat_sse",
    "wrap_tool_definition",
]
