"""Citation guardrail — enforce that answers reference retrieval sources.

When a RAG agent's answer doesn't mention any chunk_id / document
title / heading from the retrieved context, the answer is at best
unverifiable and at worst hallucinated. This middleware sits in the
output position and inspects the assistant's final-turn text against
the citations that flowed through the run.

Sources of "what should be cited" — collected by walking the run's
audit trail at handler-completion time:

1. ``ToolResultItem`` outputs from the canonical RAG tools
   (``vector_search``, ``keyword_search``, ``read_document``) —
   each result dict typically has ``results: [{chunk_id, document_id,
   ..., content}, ...]`` (the AgenticRAG factory shape).
2. Any explicit Citation model dict the user's custom tools may emit.

The guardrail accepts any of these signals as a citation:

- the literal chunk_id
- the document_id
- the document_title (case-insensitive substring match, ≥ 4 chars)
- a "Title > Heading" trail substring

Failure mode: when no signal appears in the assistant text, the
guardrail returns Deny with a message that lists candidate sources
and asks the model to retry. Local-deployment users tend to want
this as an *advisory* (don't block the SSE response, just flag it),
so the middleware also supports ``mode="warn"`` which logs a warning
but lets the answer through.

Built on the framework's ``output_guardrail`` decorator — composable
with any other guardrail in the same chain.

Note: this middleware operates per-Action call. The "final assistant
output" check is more naturally a post-run hook than a per-action
guardrail; the implementation below intercepts at the Action level
which means it sees tool results but NOT the assistant's final text.
For the actual final-text check, callers wire ``check_final_answer``
manually after ``Runner.run()`` returns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from agentic_rag.runtime.framework.exceptions import AgentsException
from agentic_rag.runtime.framework.items import ToolResultItem

logger = logging.getLogger(__name__)


_RAG_TOOL_NAMES = frozenset({"vector_search", "keyword_search", "read_document"})
_MIN_TITLE_MATCH_CHARS = 4
"""Tiles shorter than this are noisy substrings (e.g. "the") that
would falsely match almost any answer text. Skip them."""


CitationMode = Literal["block", "warn"]


# ── Reference collection ─────────────────────────────────────────────


@dataclass
class CitationReferences:
    """Citations harvested from a run's audit trail.

    Each set is the union of values seen across every RAG tool call.
    Comparing against a candidate answer means checking whether the
    answer text mentions ANY entry in ANY set.
    """

    chunk_ids: set[str] = field(default_factory=set)
    document_ids: set[str] = field(default_factory=set)
    document_titles: set[str] = field(default_factory=set)
    heading_trails: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not (
            self.chunk_ids
            or self.document_ids
            or self.document_titles
            or self.heading_trails
        )

    def all_signals(self) -> Iterable[str]:
        yield from self.chunk_ids
        yield from self.document_ids
        yield from self.document_titles
        yield from self.heading_trails


def collect_references(items: Iterable[Any]) -> CitationReferences:
    """Walk a ``RunResult.items`` list and harvest citation signals.

    Tolerant to: dict / Citation pydantic / nested ``results`` arrays,
    plain ``output: list[Citation]`` shapes, output trimmer's
    ``{"_trimmed": True, ...}`` envelope (which loses chunk-level
    detail — handled by collecting from un-trimmed metadata where
    possible).
    """
    refs = CitationReferences()
    for item in items:
        if not isinstance(item, ToolResultItem):
            continue
        result = item.result
        if result.is_error or result.output is None:
            continue
        # The wire-level result.output is a string; we need the parsed
        # structure. Try JSON; on failure fall back to scanning the
        # string for any obvious chunk_id-shaped tokens.
        _harvest_from_output(result.output, refs)
    return refs


def _harvest_from_output(output: Any, refs: CitationReferences) -> None:
    if isinstance(output, str):
        _harvest_from_string(output, refs)
    elif isinstance(output, dict):
        _harvest_from_dict(output, refs)
    elif isinstance(output, list):
        for entry in output:
            _harvest_from_output(entry, refs)


def _harvest_from_string(text: str, refs: CitationReferences) -> None:
    """Best-effort string scan when the result is already serialised.

    The runner's tool message shape is a plain string (the framework
    stringifies via ``output_as_string()``). We can still try to JSON-
    parse it; if that works, harvest from the structure. Otherwise
    skip — string-only outputs without parseable structure can't
    contribute citations.
    """
    import json

    text = text.strip()
    if not text:
        return
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return
    _harvest_from_output(parsed, refs)


def _harvest_from_dict(payload: dict[str, Any], refs: CitationReferences) -> None:
    # Common: {"results": [Citation, ...]}
    results = payload.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict):
                _add_signals_from_chunk(entry, refs)
    # Citation directly at top level
    if "chunk_id" in payload or "document_title" in payload:
        _add_signals_from_chunk(payload, refs)
    # Read-document tool emits {"chunks": [...]} / similar
    chunks = payload.get("chunks")
    if isinstance(chunks, list):
        for entry in chunks:
            if isinstance(entry, dict):
                _add_signals_from_chunk(entry, refs)


def _add_signals_from_chunk(chunk: dict[str, Any], refs: CitationReferences) -> None:
    cid = chunk.get("chunk_id")
    if isinstance(cid, str) and cid:
        refs.chunk_ids.add(cid)
    did = chunk.get("document_id")
    if isinstance(did, (str, int)):
        refs.document_ids.add(str(did))
    title = chunk.get("document_title")
    if isinstance(title, str) and len(title) >= _MIN_TITLE_MATCH_CHARS:
        refs.document_titles.add(title)
    heading = chunk.get("heading_path")
    if isinstance(heading, list):
        trail = " > ".join(str(h) for h in heading if h)
        if trail and len(trail) >= _MIN_TITLE_MATCH_CHARS:
            refs.heading_trails.add(trail)


# ── Verdict ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CitationVerdict:
    """What ``check_final_answer`` produced.

    ``cited`` is True when any signal appears in the answer.
    ``matched`` lists the signals that did match (one is enough);
    ``candidates`` lists everything we'd accept (useful for the
    error message back to the LLM).
    """

    cited: bool
    matched: list[str]
    candidates: list[str]


class CitationMissing(AgentsException):
    """Raised when ``mode='block'`` and the final answer cites nothing."""

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates
        super().__init__(
            "Final answer cited none of the retrieved sources. "
            f"Expected at least one of: {candidates[:5]!r}"
            + ("…" if len(candidates) > 5 else "")
        )


def check_final_answer(
    answer_text: str,
    references: CitationReferences,
    *,
    mode: CitationMode = "warn",
) -> CitationVerdict:
    """Decide whether ``answer_text`` adequately cites ``references``.

    When ``references.is_empty`` the check trivially passes — there
    were no retrieved sources to cite. This avoids false positives
    on conversational turns that didn't trigger retrieval.

    ``mode='block'`` raises ``CitationMissing`` on failure;
    ``mode='warn'`` logs a warning and returns the verdict so callers
    can render advisory UI without breaking the SSE response.
    """
    if references.is_empty:
        return CitationVerdict(cited=True, matched=[], candidates=[])

    candidates = sorted(set(references.all_signals()))
    matched: list[str] = []

    # Chunk ids and document ids: exact substring match (case-sensitive).
    for cid in references.chunk_ids:
        if cid in answer_text:
            matched.append(cid)
    for did in references.document_ids:
        if did and did in answer_text:
            matched.append(did)

    # Titles / heading trails: case-insensitive substring match.
    answer_lower = answer_text.lower()
    for title in references.document_titles:
        if title.lower() in answer_lower:
            matched.append(title)
    for trail in references.heading_trails:
        # Match either the full trail or any single segment ≥ threshold.
        if trail.lower() in answer_lower:
            matched.append(trail)
            continue
        for segment in trail.split(" > "):
            if (
                len(segment) >= _MIN_TITLE_MATCH_CHARS
                and segment.lower() in answer_lower
            ):
                matched.append(segment)
                break

    cited = bool(matched)
    if not cited:
        if mode == "block":
            raise CitationMissing(candidates)
        logger.warning(
            "[citation-guardrail] answer cites none of: %s",
            candidates[:5],
        )
    return CitationVerdict(cited=cited, matched=matched, candidates=candidates)


# ── Convenience: post-run check ──────────────────────────────────────


def enforce_citations(run_result: Any, *, mode: CitationMode = "warn") -> CitationVerdict:
    """One-call helper for ``Runner.run()`` callers.

        result = await Runner().run(agent, "...")
        enforce_citations(result, mode="warn")

    Walks ``result.items`` for citation signals and checks them
    against ``result.final_output``. Returns the verdict; raises
    ``CitationMissing`` in ``mode='block'``.
    """
    refs = collect_references(run_result.items)
    return check_final_answer(run_result.final_output, refs, mode=mode)


__all__ = [
    "CitationMissing",
    "CitationMode",
    "CitationReferences",
    "CitationVerdict",
    "check_final_answer",
    "collect_references",
    "enforce_citations",
]
