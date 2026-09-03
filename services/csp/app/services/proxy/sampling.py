"""Per-model thinking + sampling overrides at the CSP proxy boundary.

Caller-supplied keys win (same rule as router ``REQUEST_SAMPLING``).
``thinking_effort`` of NULL / ``default`` leaves vendor knobs alone.

This fleet's Qwen / vLLM+litellm path documents thinking via
``chat_template_kwargs.enable_thinking`` (see
``infra/models/docker-compose.yml`` ``--default-chat-template-kwargs``).
We do not invent a top-level ``enable_thinking`` key.

Thinking models that loop ("OK I'll stop") typically need ~0.6 / 0.95 /
1.5 on temperature / top_p / presence_penalty — set those columns rather
than a numeric thinking budget (owner: budget is optional advanced).
"""

from __future__ import annotations

from typing import Any, Mapping

THINKING_LEVELS = frozenset(
    {"default", "off", "low", "medium", "high", "xhigh", "max"}
)
SAMPLING_KEYS = ("temperature", "top_p", "presence_penalty", "max_tokens")

# OpenAI public ``reasoning_effort`` is low/medium/high. Repo has no
# ``xhigh`` string, so xhigh/max clamp to high.
_REASONING_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}

_SKIP_MODEL_TYPES = frozenset({"agent", "embedding", "image", "asr"})


def _looks_like_openai_reasoning_model(
    model_name: str | None, body: Mapping[str, Any]
) -> bool:
    if "reasoning_effort" in body:
        return True
    name = (model_name or "").lower()
    tokens = name.replace("/", "-").replace("_", "-").split("-")
    if any(
        t in {"o1", "o3", "o4"} or t.startswith(("o1", "o3", "o4"))
        for t in tokens
    ):
        return True
    if "gpt-5" in name:
        return True
    return False


def _set_chat_template_thinking(body: dict[str, Any], enabled: bool) -> None:
    existing = body.get("chat_template_kwargs")
    if isinstance(existing, dict):
        if "enable_thinking" in existing:
            return
        merged = dict(existing)
        merged["enable_thinking"] = enabled
        body["chat_template_kwargs"] = merged
        return
    if "chat_template_kwargs" in body:
        return
    body["chat_template_kwargs"] = {"enable_thinking": enabled}


def _apply_thinking_effort(body: dict[str, Any], model: Any) -> None:
    raw = getattr(model, "thinking_effort", None)
    if raw is None:
        return
    level = str(raw).strip().lower()
    if not level or level == "default":
        return

    if level == "off":
        _set_chat_template_thinking(body, False)
        # o-style: caller already sent reasoning_effort → leave it.
        # model name suggests o-style and no key → omit (do not add
        # "low", which would turn thinking back on).
        return

    if level not in _REASONING_EFFORT_MAP:
        return

    _set_chat_template_thinking(body, True)
    if _looks_like_openai_reasoning_model(getattr(model, "name", None), body):
        if "reasoning_effort" not in body:
            body["reasoning_effort"] = _REASONING_EFFORT_MAP[level]


def apply_model_sampling_overrides(
    request_body: Mapping[str, Any], model: Any
) -> dict[str, Any]:
    """Return a shallow copy of ``request_body`` with per-model defaults.

    Sampling keys are filled only when absent from the caller body.
    ``thinking_effort`` adds vendor knobs; it never overwrites a caller
    ``enable_thinking`` already inside ``chat_template_kwargs``.
    """
    body = dict(request_body)
    model_type = getattr(model, "model_type", None) or ""
    protocol = (getattr(model, "protocol", None) or "openai_compatible").strip()
    if protocol == "triton_grpc" or model_type in _SKIP_MODEL_TYPES:
        return body

    for key in SAMPLING_KEYS:
        if key in body:
            continue
        value = getattr(model, key, None)
        if value is not None:
            body[key] = value

    _apply_thinking_effort(body, model)
    return body
