"""Per-model thinking + sampling overrides at the CSP proxy boundary.

Caller-supplied keys win (same rule as router ``REQUEST_SAMPLING``).
``thinking_effort`` of NULL / ``default`` leaves vendor knobs alone.

A level sends BOTH vendor knobs, because this fleet's two backends read
different ones (2026-09-03, measured against the live endpoints):

* Qwen behind litellm (``…:4000/v1`` → hosted_vllm) honours top-level
  ``reasoning_effort`` and accepts ONLY ``low`` / ``medium`` / ``xhigh``
  (its default), plus ``none``; ``high`` / ``max`` / ``minimal`` come back
  400. Reasoning tokens measured: xhigh 1005, medium 153, low 104, none 0.
* gemma on bare vLLM ignores ``reasoning_effort`` completely — it swallows
  even a bogus value, and ``none`` does not stop it thinking — and reads
  only ``chat_template_kwargs.enable_thinking``.

So a level sets ``enable_thinking=true`` AND sends the level verbatim as
``reasoning_effort``. Two rules follow from the measurements above: the
level is never remapped (an earlier version clamped xhigh/max to ``high``,
which is precisely the value Qwen rejects), and the model NAME is never
consulted (it cannot tell these two backends apart — the old o1/o3/gpt-5
sniff meant Qwen silently ran at its xhigh default no matter what the
console showed). Which levels an endpoint actually accepts is settled by
the save-time probe in ``app/services/thinking_probe.py``.

``none`` / ``off`` sets ``enable_thinking=false`` and sends NO
``reasoning_effort``: gemma ignores it anyway, and Qwen is already
silenced by the chat-template kwarg.

We do not invent a top-level ``enable_thinking`` key — the kwargs form is
what ``infra/models/docker-compose.yml`` documents
(``--default-chat-template-kwargs``).

Thinking models that loop ("OK I'll stop") typically need ~0.6 / 0.95 /
1.5 on temperature / top_p / presence_penalty — set those columns rather
than a numeric thinking budget (owner: budget is optional advanced).
"""

from __future__ import annotations

from typing import Any, Mapping

THINKING_LEVELS = frozenset(
    {"none", "off", "default", "low", "medium", "high", "xhigh", "max"}
)
SAMPLING_KEYS = ("temperature", "top_p", "presence_penalty", "max_tokens")

# The Router always sends temperature / max_tokens (its ``router`` sampling
# row). It flags which of those are its own defaults in this body-only
# marker so the model_registry knobs can still win over them; a real
# caller value (no marker entry) keeps winning. Never forwarded upstream.
SAMPLING_DEFAULTS_MARKER = "anila_sampling_defaults"

# Levels that turn thinking ON; each is sent verbatim as
# ``reasoning_effort``. No remap table — see the module docstring.
THINKING_ON_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

# Conversation / per-turn picker values. Stored as tiers, mapped here.
THINKING_TIERS = frozenset({"default", "off", "standard", "deep"})
ANILA_THINKING_TIER_KEY = "anila_thinking_tier"
_STANDARD_PREFERENCE = ("medium", "low", "high")
_DEEP_PREFERENCE = ("max", "xhigh", "high", "medium", "low")

_SKIP_MODEL_TYPES = frozenset({"agent", "embedding", "image", "asr"})


def _normalize_thinking_tier(tier: Any) -> str | None:
    if tier is None:
        return None
    value = str(tier).strip().lower()
    if not value or value == "default":
        return None
    return value


def _supported_level_set(supported: Any) -> set[str] | None:
    if supported is None:
        return None
    if isinstance(supported, str):
        return {supported.strip().lower()} if supported.strip() else set()
    try:
        return {str(item).strip().lower() for item in supported if str(item).strip()}
    except TypeError:
        return None


def resolve_thinking_level(
    tier: Any,
    supported: Any,
    model_default: Any,
) -> tuple[str | None, bool]:
    """Map a user picker tier onto ``(reasoning_effort or None, enable_thinking)``.

    ``default`` / ``None`` is a no-op: the caller falls back to
    ``_apply_thinking_effort``. ``model_default`` is accepted for the
    documented signature; default-tier mapping does not use it.
    """
    del model_default  # default / None never remaps through this table
    normalized = _normalize_thinking_tier(tier)
    if normalized is None:
        return (None, False)
    if normalized == "off":
        return (None, False)

    levels = _supported_level_set(supported)
    only_none = levels is not None and (not levels or levels <= {"none"})

    if normalized == "standard":
        if levels is None or only_none:
            return (None, True)
        for candidate in _STANDARD_PREFERENCE:
            if candidate in levels:
                return (candidate, True)
        return (None, True)

    if normalized == "deep":
        if levels is None:
            return (None, True)
        for candidate in _DEEP_PREFERENCE:
            if candidate in levels:
                return (candidate, True)
        return (None, True)

    return (None, False)


def is_thinking_locked(model: Any) -> bool:
    return getattr(model, "thinking_user_selectable", True) is False


def stamp_thinking_locked(meta: dict[str, Any] | None, model: Any) -> None:
    if isinstance(meta, dict) and is_thinking_locked(model):
        meta["thinking_locked"] = True


def _caller_supplied_thinking(body: Mapping[str, Any]) -> bool:
    if "reasoning_effort" in body:
        return True
    kwargs = body.get("chat_template_kwargs")
    return isinstance(kwargs, dict) and "enable_thinking" in kwargs


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

    if level in {"none", "off"}:
        _set_chat_template_thinking(body, False)
        return

    if level not in THINKING_ON_LEVELS:
        return

    _set_chat_template_thinking(body, True)
    if "reasoning_effort" not in body:
        body["reasoning_effort"] = level


def apply_model_sampling_overrides(
    request_body: Mapping[str, Any],
    model: Any,
    *,
    thinking_tier: Any = None,
) -> dict[str, Any]:
    """Return a shallow copy of ``request_body`` with per-model defaults.

    Sampling keys are filled only when absent from the caller body.
    ``thinking_effort`` adds vendor knobs; it never overwrites a caller
    ``enable_thinking`` already inside ``chat_template_kwargs``.

    Thinking-tier priority:

    1. Caller already sent ``reasoning_effort`` or ``enable_thinking``
    2. Body ``anila_thinking_tier`` (always popped; never forwarded)
    3. ``thinking_tier`` from the conversation row (non-default)
    4. Model row ``thinking_effort`` (current ``_apply_thinking_effort``)

    ``thinking_user_selectable=False`` ignores 2 and 3.
    """
    body = dict(request_body)
    marker = body.pop(SAMPLING_DEFAULTS_MARKER, None)
    body.pop(ANILA_THINKING_TIER_KEY, None)
    body_tier = request_body.get(ANILA_THINKING_TIER_KEY)
    soft_keys = (
        {k for k in marker if isinstance(k, str)}
        if isinstance(marker, (list, tuple))
        else set()
    )
    model_type = getattr(model, "model_type", None) or ""
    protocol = (getattr(model, "protocol", None) or "openai_compatible").strip()
    if protocol == "triton_grpc" or model_type in _SKIP_MODEL_TYPES:
        return body

    for key in SAMPLING_KEYS:
        if key in body and key not in soft_keys:
            continue
        value = getattr(model, key, None)
        if value is not None:
            body[key] = value

    if _caller_supplied_thinking(body) or is_thinking_locked(model):
        _apply_thinking_effort(body, model)
        return body

    effective = body_tier if body_tier is not None else thinking_tier
    normalized = _normalize_thinking_tier(effective)
    if normalized in {"off", "standard", "deep"}:
        level, enable = resolve_thinking_level(
            effective,
            getattr(model, "thinking_levels_supported", None),
            getattr(model, "thinking_effort", None),
        )
        _set_chat_template_thinking(body, enable)
        if level is not None:
            body["reasoning_effort"] = level
        return body

    _apply_thinking_effort(body, model)
    return body
