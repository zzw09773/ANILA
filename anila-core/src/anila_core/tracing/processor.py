"""Span processors — endpoints that receive finished spans.

Plug your own (Jaeger / OTel-collector / Prometheus / DB) by
implementing the :class:`SpanProcessor` Protocol. Anila ships an
:class:`InMemoryProcessor` that buffers spans for tests and for
serialising into ``anila_meta.spans`` on the response.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .span import Span


@runtime_checkable
class SpanProcessor(Protocol):
    """Receives finished spans. Called from :class:`Tracer.end_span`."""

    def on_end(self, span: Span) -> None:
        ...


class InMemoryProcessor:
    """Collect closed spans into a list for tests / response serialisation."""

    def __init__(self) -> None:
        self._spans: list[Span] = []

    def on_end(self, span: Span) -> None:
        self._spans.append(span)

    @property
    def spans(self) -> list[Span]:
        """All collected spans, in close-time order."""
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()

    def to_tree(self) -> list[dict[str, Any]]:
        """Render the collected spans as a parent-rooted nested tree.

        Top-level entries are spans without a parent_id (or whose parent
        wasn't seen). Each entry has its serialised dict plus a
        ``children`` list of nested entries, recursively.
        """
        by_id: dict[str, dict[str, Any]] = {
            s.span_id: {**s.to_dict(), "children": []} for s in self._spans
        }
        roots: list[dict[str, Any]] = []
        for span in self._spans:
            entry = by_id[span.span_id]
            parent = (
                by_id.get(span.parent_id)
                if span.parent_id is not None
                else None
            )
            if parent is None:
                roots.append(entry)
            else:
                parent["children"].append(entry)
        return roots
