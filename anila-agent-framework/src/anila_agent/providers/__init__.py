"""LLM provider abstraction.

The framework talks to LLMs through ``LLMProvider`` Protocol — anything
that satisfies the shape works. This decouples the run loop from any
specific vendor SDK, so vLLM / OpenAI / Anthropic / NIM / Ollama are
interchangeable behind the Protocol.

v0.1 ships:
  - ``protocol.LLMProvider`` — the Protocol itself
  - (Sprint 1 stage B) ``openai_compat.OpenAICompatProvider`` —
    Chat Completions impl that covers OpenAI / vLLM / NIM / TGI / Ollama

v0.3 may add:
  - ``anthropic.AnthropicProvider`` — Messages API
  - ``responses.OpenAIResponsesProvider`` — OpenAI Responses API
"""

from anila_agent.providers.protocol import LLMProvider

__all__ = ["LLMProvider"]
