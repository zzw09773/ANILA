"""Runtime backend abstraction layer.

Decouples anila-agent from the openai-agents SDK so that future backends
(Raw HTTP on vLLM, other agent frameworks) can plug in without rewriting
hooks / policy / trigger code.

Public surface:

- ``Message`` / ``Response`` / ``Chunk`` / ``ToolCall`` / ``Usage`` — SDK-agnostic
  dataclasses that mirror the OpenAI chat-completion shape.
- ``ConnectionStrategy`` — ABC every backend implementation honours.
- ``OpenAIAgentsConnection`` — the default backend, wraps the existing
  ``from agents import ...`` flow.
- ``ConnectionRegistry`` — name → strategy factory lookup, defaults to
  ``"openai_agents"``.

The existing :class:`anila_agent.core.runner.AnilaRunner` is **not** rewired by
this module. ConnectionStrategy is a parallel abstraction; the runner rewrite
lands in a later sprint.
"""

from __future__ import annotations

from anila_agent.runtime.connection import ConnectionStrategy
from anila_agent.runtime.openai_agents_connection import OpenAIAgentsConnection
from anila_agent.runtime.registry import ConnectionRegistry, default_registry
from anila_agent.runtime.types import Chunk, Message, Response, ToolCall, Usage

__all__ = [
    "Chunk",
    "ConnectionRegistry",
    "ConnectionStrategy",
    "Message",
    "OpenAIAgentsConnection",
    "Response",
    "ToolCall",
    "Usage",
    "default_registry",
]
