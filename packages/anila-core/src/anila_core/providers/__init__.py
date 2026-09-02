"""Provider abstractions and implementations.

2026-09-02: ``base`` stays eager (the Router uses it); ``MockProvider`` /
``ScriptedResponse`` / ``OpenAICompatProvider`` resolve lazily (PEP 562) so
processes that never build a provider do not load them.
"""

from __future__ import annotations

import importlib

from .base import Provider, ProviderRequest

_LAZY: dict[str, str] = {
    "MockProvider": ".mock",
    "ScriptedResponse": ".mock",
    "OpenAICompatProvider": ".openai_compat",
}

__all__ = ["Provider", "ProviderRequest", *sorted(_LAZY)]


def __getattr__(name: str):
    try:
        modname = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(modname, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
