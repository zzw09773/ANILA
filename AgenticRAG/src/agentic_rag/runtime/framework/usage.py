"""Provider-agnostic token / request usage tracking.

Originally usage.py from openai-agents-python (MIT); rewritten here to
drop ``openai.types.completion_usage`` and ``openai.types.responses.
response_usage`` dependencies. The shape stays compatible — same field
names — but the types are plain dataclasses instead of OpenAI SDK
imports. Providers that emit OpenAI-shape usage payloads can be
mapped via ``Usage.from_openai_payload(...)`` (added when the OpenAI
provider lands in Sprint 1 stage B).

Why we keep the same shape:

- AgenticRAG's existing token-usage logging speaks this dialect already
- Tracing span data follows the same contract
- Cost calculation downstream (CSP usage table) reads these fields

What's different from upstream:

- ``InputTokensDetails`` / ``OutputTokensDetails`` are local dataclasses,
  not OpenAI SDK types
- No ``BeforeValidator`` magic — we rely on simple ``__post_init__`` for
  the None-guard cases that come up with non-OpenAI providers
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


# ── Token detail dataclasses ─────────────────────────────────────────────

@dataclass
class InputTokensDetails:
    """Breakdown of input-side tokens.

    Mirrors OpenAI's ``InputTokensDetails`` but without the SDK import.
    ``cached_tokens`` is the number of input tokens served from a
    provider-side prompt cache (lower cost per token); 0 when the
    provider doesn't expose this.
    """

    cached_tokens: int = 0


@dataclass
class OutputTokensDetails:
    """Breakdown of output-side tokens.

    ``reasoning_tokens`` is the count of tokens spent on internal
    chain-of-thought (only meaningful for reasoning-capable models);
    0 otherwise.
    """

    reasoning_tokens: int = 0


# ── Per-request usage ─────────────────────────────────────────────────────

@dataclass
class RequestUsage:
    """Token usage for a single LLM API call.

    Aggregating these in ``Usage.request_usage_entries`` lets cost
    calculation see per-call breakdowns (rather than just a flat
    aggregate), which matters when a single run makes calls of
    very different sizes.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_tokens_details: InputTokensDetails = field(
        default_factory=lambda: InputTokensDetails(cached_tokens=0)
    )
    output_tokens_details: OutputTokensDetails = field(
        default_factory=lambda: OutputTokensDetails(reasoning_tokens=0)
    )


# ── Aggregate usage across a run ─────────────────────────────────────────

@dataclass
class Usage:
    """Aggregate token / request usage across a run.

    Use ``Usage()`` as the zero value, ``add()`` to accumulate
    per-call numbers. ``request_usage_entries`` preserves per-call
    breakdowns so cost calculation can replay the call sequence
    rather than only seeing the totals.
    """

    requests: int = 0
    """Total LLM API requests made."""

    input_tokens: int = 0
    """Total input tokens sent across all requests."""

    input_tokens_details: InputTokensDetails = field(
        default_factory=lambda: InputTokensDetails(cached_tokens=0)
    )

    output_tokens: int = 0
    """Total output tokens received across all requests."""

    output_tokens_details: OutputTokensDetails = field(
        default_factory=lambda: OutputTokensDetails(reasoning_tokens=0)
    )

    total_tokens: int = 0
    """input_tokens + output_tokens."""

    request_usage_entries: list[RequestUsage] = field(default_factory=list)
    """Per-call breakdown — one entry per LLM request that contributed
    non-zero tokens. Cost calculation reads this for per-call accuracy."""

    def __post_init__(self) -> None:
        # Some providers leave optional detail fields unset; coerce to 0
        # so downstream arithmetic doesn't NoneType-explode.
        if self.input_tokens_details is None:
            self.input_tokens_details = InputTokensDetails(cached_tokens=0)
        if self.output_tokens_details is None:
            self.output_tokens_details = OutputTokensDetails(reasoning_tokens=0)

    def add(self, other: Usage) -> None:
        """Aggregate ``other`` into ``self`` in place.

        Single-request ``other`` (i.e. ``other.requests == 1`` and total
        tokens > 0) gets recorded as a new ``RequestUsage`` entry so
        per-call breakdowns survive aggregation. Multi-request ``other``
        already has its own entries — those are extended in.
        """
        self.requests += other.requests or 0
        self.input_tokens += other.input_tokens or 0
        self.output_tokens += other.output_tokens or 0
        self.total_tokens += other.total_tokens or 0

        other_cached = (
            other.input_tokens_details.cached_tokens
            if other.input_tokens_details and other.input_tokens_details.cached_tokens
            else 0
        )
        other_reasoning = (
            other.output_tokens_details.reasoning_tokens
            if other.output_tokens_details and other.output_tokens_details.reasoning_tokens
            else 0
        )
        self_cached = (
            self.input_tokens_details.cached_tokens
            if self.input_tokens_details and self.input_tokens_details.cached_tokens
            else 0
        )
        self_reasoning = (
            self.output_tokens_details.reasoning_tokens
            if self.output_tokens_details and self.output_tokens_details.reasoning_tokens
            else 0
        )

        self.input_tokens_details = InputTokensDetails(
            cached_tokens=self_cached + other_cached
        )
        self.output_tokens_details = OutputTokensDetails(
            reasoning_tokens=self_reasoning + other_reasoning
        )

        if other.requests == 1 and other.total_tokens > 0:
            self.request_usage_entries.append(
                RequestUsage(
                    input_tokens=other.input_tokens,
                    output_tokens=other.output_tokens,
                    total_tokens=other.total_tokens,
                    input_tokens_details=other.input_tokens_details
                    or InputTokensDetails(cached_tokens=0),
                    output_tokens_details=other.output_tokens_details
                    or OutputTokensDetails(reasoning_tokens=0),
                )
            )
        elif other.request_usage_entries:
            self.request_usage_entries.extend(other.request_usage_entries)


# ── Serialization ────────────────────────────────────────────────────────

def serialize_usage(usage: Usage) -> dict[str, Any]:
    """Serialize a Usage object into a JSON-friendly dict.

    Shape matches what consumers (CSP usage table, tracing, evaluator)
    already expect, so this is wire-compatible with the upstream
    serialiser.
    """
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "input_tokens_details": [{"cached_tokens": usage.input_tokens_details.cached_tokens}],
        "output_tokens": usage.output_tokens,
        "output_tokens_details": [
            {"reasoning_tokens": usage.output_tokens_details.reasoning_tokens}
        ],
        "total_tokens": usage.total_tokens,
        "request_usage_entries": [
            {
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "total_tokens": e.total_tokens,
                "input_tokens_details": {"cached_tokens": e.input_tokens_details.cached_tokens},
                "output_tokens_details": {
                    "reasoning_tokens": e.output_tokens_details.reasoning_tokens
                },
            }
            for e in usage.request_usage_entries
        ],
    }


def deserialize_usage(usage_data: Mapping[str, Any]) -> Usage:
    """Rebuild a Usage object from a serialised dict.

    Tolerant of missing optional fields: providers that don't emit the
    detail breakdowns get zeroes back, not None.
    """

    def _details(raw: Any, kind: str) -> Any:
        # Upstream serialises these as a single-element list; tolerate
        # both list and dict shapes.
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if not isinstance(raw, Mapping):
            raw = {}
        if kind == "input":
            return InputTokensDetails(cached_tokens=int(raw.get("cached_tokens") or 0))
        return OutputTokensDetails(reasoning_tokens=int(raw.get("reasoning_tokens") or 0))

    entries: list[RequestUsage] = []
    for entry in usage_data.get("request_usage_entries") or []:
        entries.append(
            RequestUsage(
                input_tokens=int(entry.get("input_tokens") or 0),
                output_tokens=int(entry.get("output_tokens") or 0),
                total_tokens=int(entry.get("total_tokens") or 0),
                input_tokens_details=_details(entry.get("input_tokens_details"), "input"),
                output_tokens_details=_details(entry.get("output_tokens_details"), "output"),
            )
        )

    return Usage(
        requests=int(usage_data.get("requests") or 0),
        input_tokens=int(usage_data.get("input_tokens") or 0),
        output_tokens=int(usage_data.get("output_tokens") or 0),
        total_tokens=int(usage_data.get("total_tokens") or 0),
        input_tokens_details=_details(usage_data.get("input_tokens_details"), "input"),
        output_tokens_details=_details(usage_data.get("output_tokens_details"), "output"),
        request_usage_entries=entries,
    )
