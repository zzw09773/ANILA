"""
ANILA Core — Python Agent Runtime

Phase 0 Boundaries
==================
Core responsibility:
  - Agent orchestration
  - Tool execution
  - History / compact
  - Memory lifecycle (extraction → selection → consolidation)
  - Provider abstraction
  - RAG orchestration

NOT responsible for:
  - H100 deployment
  - vLLM process management
  - Company auth / monitoring

Storage key structure:
  All persistent data is indexed with the three-layer key:
    user_id + project_id + session_id

This maps naturally to OpenWebUI: account → workspace → conversation.
"""

__version__ = "0.1.0"
