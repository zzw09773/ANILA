"""LLM provider abstraction.

The framework talks to LLMs through ``LLMProvider`` Protocol — anything
that satisfies the shape works. This decouples the run loop from any
specific vendor SDK, so vLLM / OpenAI / Anthropic / NIM / Ollama are
interchangeable behind the Protocol.

v0.1 ships:
  - ``protocol.LLMProvider`` — the Protocol itself
  - ``openai_compat.OpenAICompatProvider`` — Chat Completions impl that
    covers OpenAI / vLLM / NIM / TGI / Ollama (requires ``[openai]`` extra)

v0.3 may add:
  - ``anthropic.AnthropicProvider`` — Messages API
  - ``responses.OpenAIResponsesProvider`` — OpenAI Responses API

The ``openai_compat`` module is import-safe even without the ``openai``
package installed; the missing dependency surfaces only when a caller
actually instantiates ``OpenAICompatProvider``.
"""

from agentic_rag.runtime.framework.providers.protocol import LLMProvider

__all__ = ["LLMProvider"]
