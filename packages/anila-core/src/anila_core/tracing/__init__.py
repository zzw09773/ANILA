"""Hierarchical tracing — OTel-style span tree for ANILA agents.

Inspired by openai-agents `tracing/` package, distilled to the parts
ANILA actually needs:

- :class:`Span` — one timed unit of work with parent / children, status,
  attributes. Frozen on close.
- :class:`Tracer` — owns the current-span context (via contextvars) and
  the list of finished spans.
- :class:`SpanProcessor` Protocol + :class:`InMemoryProcessor`
  reference impl — caller can swap in a Jaeger / OTel exporter.
- :class:`TracingHooks` — :class:`RunHooks` adapter that auto-creates
  spans for every run / agent / tool / pause / resume / handoff event,
  so callers get tracing for free by passing
  ``QueryEngine(hooks=TracingHooks(tracer))``.

Distinct from anila-core's existing ``anila_meta.trace`` (flat list of
trace steps). The flat trace stays for back-compat; spans are an
opt-in richer view for multi-agent debugging. A future PR may merge
the two by deriving the flat list from the span tree.
"""

from .hooks import TracingHooks
from .processor import InMemoryProcessor, SpanProcessor
from .span import Span, SpanKind, SpanStatus
from .tracer import Tracer

__all__ = [
    "Span",
    "SpanKind",
    "SpanStatus",
    "Tracer",
    "SpanProcessor",
    "InMemoryProcessor",
    "TracingHooks",
]
