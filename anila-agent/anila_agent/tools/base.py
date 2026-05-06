"""Anila tool wrapper around openai-agents `@function_tool`.

Adds the metadata claude-code-src tools carry: `is_read_only`, `is_destructive`,
`requires_confirmation`. Hooks read these flags to gate tool execution.

Use either the decorator (`@anila_tool`) or build a FunctionTool directly via
`AnilaTool.from_function`.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents import FunctionTool, function_tool


@dataclass(frozen=True)
class ToolMetadata:
    """Anila-level annotations attached to a tool. Read by hooks, not the model."""

    is_read_only: bool = False
    is_destructive: bool = False
    requires_confirmation: bool = False
    category: str = "general"


_METADATA_ATTR = "__anila_metadata__"


def _attach_metadata(tool: FunctionTool, metadata: ToolMetadata) -> FunctionTool:
    setattr(tool, _METADATA_ATTR, metadata)
    return tool


def get_metadata(tool: FunctionTool | Any) -> ToolMetadata:
    """Return Anila metadata for a tool, or defaults if it has none."""
    meta = getattr(tool, _METADATA_ATTR, None)
    if isinstance(meta, ToolMetadata):
        return meta
    return ToolMetadata()


def anila_tool(
    *,
    is_read_only: bool = False,
    is_destructive: bool = False,
    requires_confirmation: bool = False,
    category: str = "general",
    **function_tool_kwargs: Any,
) -> Callable[[Callable[..., Any]], FunctionTool]:
    """Decorator equivalent to `@function_tool` plus Anila metadata.

    Example:

        @anila_tool(is_read_only=True, category="retrieval")
        def search_documents(query: str, k: int = 5) -> list[dict]: ...
    """

    def decorator(fn: Callable[..., Any]) -> FunctionTool:
        wrapped = function_tool(**function_tool_kwargs)(fn)
        if not isinstance(wrapped, FunctionTool):
            raise TypeError("function_tool did not return a FunctionTool")
        return _attach_metadata(
            wrapped,
            ToolMetadata(
                is_read_only=is_read_only,
                is_destructive=is_destructive,
                requires_confirmation=requires_confirmation,
                category=category,
            ),
        )

    return decorator


@dataclass
class AnilaTool:
    """Programmatic builder for non-decorator tool registration.

    Use this when wiring a tool whose function comes from somewhere you can't decorate
    (e.g. a class method bound at runtime).
    """

    fn: Callable[..., Any]
    metadata: ToolMetadata = field(default_factory=ToolMetadata)
    name: str | None = None
    description: str | None = None

    def build(self) -> FunctionTool:
        kwargs: dict[str, Any] = {}
        if self.name is not None:
            kwargs["name_override"] = self.name
        if self.description is not None:
            kwargs["description_override"] = self.description
        wrapped = function_tool(**kwargs)(self.fn)
        if not isinstance(wrapped, FunctionTool):
            raise TypeError("function_tool did not return a FunctionTool")
        return _attach_metadata(wrapped, self.metadata)

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        is_read_only: bool = False,
        is_destructive: bool = False,
        requires_confirmation: bool = False,
        category: str = "general",
        name: str | None = None,
        description: str | None = None,
    ) -> FunctionTool:
        return cls(
            fn=fn,
            metadata=ToolMetadata(
                is_read_only=is_read_only,
                is_destructive=is_destructive,
                requires_confirmation=requires_confirmation,
                category=category,
            ),
            name=name,
            description=description,
        ).build()


@functools.lru_cache(maxsize=128)
def _import_attribute(qualified: str) -> Any:
    import importlib

    module_path, _, attr = qualified.rpartition(".")
    if not module_path:
        raise ValueError(f"Invalid import path: {qualified!r}")
    return getattr(importlib.import_module(module_path), attr)


def import_tool(qualified: str) -> FunctionTool:
    """Resolve a fully-qualified attribute path to a FunctionTool."""
    obj = _import_attribute(qualified)
    if isinstance(obj, FunctionTool):
        return obj
    if callable(obj):
        return AnilaTool.from_function(obj)
    raise TypeError(f"{qualified!r} is neither a FunctionTool nor callable")
