"""OpenAI-compatible model adapter.

Built on `agents.extensions.models.litellm_model.LitellmModel`, which speaks
any OpenAI-compatible HTTP endpoint (vLLM, Ollama, OpenAI, Together, Groq, …).
"""

from __future__ import annotations

from typing import Any

from agents import ModelSettings
from agents.extensions.models.litellm_model import LitellmModel
from agents.models.interface import Model

from anila_agent.utils.config import ModelConfig


def build_model(config: ModelConfig) -> Model:
    """Resolve a `ModelConfig` to a Model instance."""
    return LitellmModel(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
    )


def build_model_settings(config: ModelConfig) -> ModelSettings:
    """Convert the YAML `settings:` mapping to `ModelSettings`. Unknown keys are dropped."""
    raw: dict[str, Any] = dict(config.settings or {})
    allowed = {
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "max_tokens",
        "tool_choice",
        "parallel_tool_calls",
        "truncation",
        "store",
        "include_usage",
        "extra_query",
        "extra_body",
        "extra_headers",
    }
    filtered = {k: v for k, v in raw.items() if k in allowed}
    return ModelSettings(**filtered) if filtered else ModelSettings()
