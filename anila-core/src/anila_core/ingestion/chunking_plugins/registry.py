"""Chunker strategy registry (docs/ingestion-platform-design.md §5.2).

Plug-ins register at import time via ``@register_chunker``; the worker
and the API layer look strategies up by name. Built-in strategies are
auto-imported from ``builtins`` so consumers only need

    from anila_core.ingestion.chunking_plugins import get_chunker

to use them.

Registration is *idempotent* on identity but errors on name collision:
re-importing a builtin module is fine, but two different classes
claiming the same ``name`` is a wiring bug worth failing fast on.
"""

from __future__ import annotations

from typing import Any

from anila_core.ingestion.chunking_plugins.base import ChunkerStrategy

# name -> strategy class. Populated by @register_chunker imports.
_REGISTRY: dict[str, type[ChunkerStrategy]] = {}


def register_chunker(cls: type[ChunkerStrategy]) -> type[ChunkerStrategy]:
    """Class decorator — adds ``cls`` to the global registry.

    Re-decorating the same class is a no-op (matters for repeated
    test imports / hot-reload). Re-using ``name`` for a different class
    raises ``ValueError`` because that would silently mask the original.
    """
    existing = _REGISTRY.get(cls.name)
    if existing is None:
        _REGISTRY[cls.name] = cls
    elif existing is cls:
        pass  # idempotent re-import; tolerate.
    else:
        raise ValueError(
            f"Chunker name {cls.name!r} already registered to "
            f"{existing.__module__}.{existing.__name__}; refusing to "
            f"silently override with {cls.__module__}.{cls.__name__}."
        )
    return cls


def get_chunker(name: str) -> ChunkerStrategy:
    """Return a fresh strategy instance for ``name``.

    Raises ``KeyError`` (with a helpful list) when the name is unknown —
    that surface area is what the API layer needs to translate into a
    422 with the available strategies in the body.

    Strategies are stateless from the registry's POV, so we instantiate
    here without args; per-call params land at ``chunk()`` time, not
    construction. This avoids per-collection caching surprises if a
    plug-in author ever leaks state into ``__init__``.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown chunker {name!r}. Registered: {sorted(_REGISTRY)}"
        )
    return cls()


def list_chunkers() -> list[dict[str, Any]]:
    """Public catalog used by ``GET /api/ingestion/chunkers``.

    The shape matches what the dev UI needs to render the strategy
    picker: id, label, defaults, JSON-schema for params. Order is
    insertion order (= module import order) so the most foundational
    strategy lists first when builtins are imported in dependency order.
    """
    return [
        {
            "name": cls.name,
            "display_name": cls.display_name,
            "default_params": dict(cls.default_params),
            "param_schema": dict(cls.param_schema),
            # Surface the class-level flag so the chunking-preview API can
            # filter out chunkers that need pre-computed embeddings (e.g.
            # ``semantic``) — without this, preview tries to run them and
            # the chunker raises a confusing "missing _segments" error.
            "requires_embedder": getattr(cls, "requires_embedder", False),
        }
        for cls in _REGISTRY.values()
    ]


def _clear_registry_for_tests() -> None:
    """Test-only escape hatch.

    Prefixed underscore + explicit name: anyone reading a stack trace
    will know this is not for production use. Tests that monkey-patch
    or reimport plug-ins call this to start from a clean slate.
    """
    _REGISTRY.clear()
