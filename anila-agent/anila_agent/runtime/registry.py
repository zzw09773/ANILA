"""``ConnectionRegistry`` — name → :class:`ConnectionStrategy` factory lookup.

The registry is the integration point between higher-level config
(``runtime.backend: "openai_agents"`` in YAML) and concrete strategies. It
intentionally stays tiny:

- ``register(name, factory)`` — bind a name to a callable that produces a
  ``ConnectionStrategy`` instance.
- ``get(name=None, **kwargs)`` — build an instance using the registered
  factory. ``name=None`` resolves the current default backend.
- ``set_default(name)`` — switch which name ``get(None)`` returns.

A module-level :data:`default_registry` is pre-seeded with the
``"openai_agents"`` factory so the common path requires zero extra
configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from anila_agent.runtime.connection import ConnectionStrategy
from anila_agent.runtime.openai_agents_connection import OpenAIAgentsConnection

StrategyFactory = Callable[..., ConnectionStrategy]

DEFAULT_BACKEND_NAME: str = "openai_agents"


class ConnectionRegistry:
    """Mutable registry of backend factories."""

    def __init__(self, *, default: str = DEFAULT_BACKEND_NAME) -> None:
        self._factories: dict[str, StrategyFactory] = {}
        self._default = default

    def register(self, name: str, factory: StrategyFactory) -> None:
        """Bind ``name`` to a factory.

        Re-registering an existing name overwrites the previous binding so
        callers can swap strategies in tests without juggling registry
        instances.
        """
        if not name:
            raise ValueError("backend name must be a non-empty string")
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def has(self, name: str) -> bool:
        return name in self._factories

    @property
    def default(self) -> str:
        return self._default

    def set_default(self, name: str) -> None:
        if name not in self._factories:
            raise KeyError(f"backend {name!r} is not registered")
        self._default = name

    def names(self) -> tuple[str, ...]:
        return tuple(self._factories.keys())

    def get(self, name: str | None = None, **kwargs: Any) -> ConnectionStrategy:
        """Instantiate a strategy.

        Args:
            name: Backend identifier. ``None`` falls back to the registry's
                default (``"openai_agents"`` unless overridden).
            **kwargs: Forwarded to the registered factory.

        Returns:
            A fresh :class:`ConnectionStrategy` instance.

        Raises:
            KeyError: If ``name`` (or the default) is not registered.
        """
        resolved = name if name is not None else self._default
        try:
            factory = self._factories[resolved]
        except KeyError as exc:
            raise KeyError(
                f"backend {resolved!r} is not registered; "
                f"known: {sorted(self._factories)}"
            ) from exc
        return factory(**kwargs)


def _default_openai_agents_factory(**kwargs: Any) -> ConnectionStrategy:
    """Built-in factory for the default backend.

    Accepts the same kwargs as :class:`OpenAIAgentsConnection`. The factory is
    purposefully thin: a single ``**kwargs`` passthrough so the registry stays
    backend-agnostic.
    """
    return OpenAIAgentsConnection(**kwargs)


default_registry: ConnectionRegistry = ConnectionRegistry()
default_registry.register(DEFAULT_BACKEND_NAME, _default_openai_agents_factory)
