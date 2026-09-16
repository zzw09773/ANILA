"""Compact OpenAI-style ``[{role, content}]`` histories (Router chat path).

Layer 2 (auto compact) asks an optional summarizer for older turns.
Layer 3 (sliding window) drops them if the summarizer is missing or fails.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .auto_compact import get_auto_compact_threshold, should_compact
from .sliding_window import SLIDING_WINDOW_SUMMARY
from .strip_images import estimate_openai_tokens_with_images, strip_images_openai

HISTORY_SUMMARY_PREFIX = "[歷史摘要]"

Summarizer = Callable[[list[dict[str, Any]]], Awaitable[str | None]]


@dataclass(frozen=True)
class CompactResult:
    messages: list[dict[str, Any]]
    compacted: bool
    method: str  # "none" | "strip_images" | "summary" | "sliding_window"
    tokens_before: int
    tokens_after: int
    summary: str | None = None
    recent_count: int = 0


def flatten_openai_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return "" if value is None else str(value)
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
            continue
        content = block.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "".join(parts)


def estimate_openai_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    return estimate_openai_tokens_with_images(messages)


def is_prompt_too_long(status_code: int | None, body: str | None) -> bool:
    if status_code not in (400, 413):
        return False
    blob = (body or "").lower()
    return any(
        needle in blob
        for needle in (
            "context_overflow",
            "context_length_exceeded",
            "context_window_exceeded",
            "contextwindowexceedederror",
            "maximum context",
            "too many tokens",
            "prompt is too long",
            "prompt too long",
            "reduce the length",
        )
    )


def _is_system(msg: Mapping[str, Any]) -> bool:
    return msg.get("role") == "system"


def _is_identity_protected(
    msg: Mapping[str, Any],
    protected: Sequence[Mapping[str, Any]],
) -> bool:
    return any(msg is item for item in protected)


def _retain_or_copy(
    msg: dict[str, Any],
    protected: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep Router-produced summary dicts by identity; copy everything else."""
    if _is_identity_protected(msg, protected):
        return msg
    return dict(msg)


def _is_tool_result(msg: Mapping[str, Any]) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _leading_system_len(messages: Sequence[Mapping[str, Any]]) -> int:
    for i, msg in enumerate(messages):
        if not _is_system(msg):
            return i
    return len(messages)


def _leading_prefix_len(
    messages: Sequence[Mapping[str, Any]],
    protected: Sequence[Mapping[str, Any]] = (),
) -> int:
    """Leading ``role=system`` rows plus identity-tracked protected objects."""
    for i, msg in enumerate(messages):
        if _is_system(msg) or _is_identity_protected(msg, protected):
            continue
        return i
    return len(messages)


def _split_turns(
    messages: list[dict[str, Any]],
    protected: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    cut = _leading_prefix_len(messages, protected)
    system = [_retain_or_copy(m, protected) for m in messages[:cut]]
    convo: list[dict[str, Any]] = []
    for msg in messages[cut:]:
        if _is_system(msg) or _is_identity_protected(msg, protected):
            system.append(_retain_or_copy(msg, protected))
        else:
            convo.append(dict(msg))

    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in convo:
        if msg.get("role") == "user" and current and not _is_tool_result(msg):
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)
    return system, turns


def _flatten_turns(turns: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for turn in turns:
        out.extend(turn)
    return out


def _new_sliding_marker() -> dict[str, Any]:
    """Router-produced placeholder. Callers track it by object identity."""
    return {"role": "user", "content": SLIDING_WINDOW_SUMMARY}


def _has_identity(
    messages: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any] | None,
) -> bool:
    return target is not None and any(m is target for m in messages)


def _recent_count_from_final(
    messages: Sequence[Mapping[str, Any]],
    summary_message: Mapping[str, Any] | None = None,
    marker_message: Mapping[str, Any] | None = None,
) -> int:
    """Inbound conversation rows left after compact (not system / summary / marker).

    Synthetic rows are subtracted only when the produced object is still present.
    A real user whose content equals ``SLIDING_WINDOW_SUMMARY`` is not a marker.
    """
    extra = 1 if _has_identity(messages, summary_message) else 0
    markers = 1 if _has_identity(messages, marker_message) else 0
    return max(0, len(messages) - _leading_system_len(messages) - extra - markers)


def _result_from_outbound(
    messages: list[dict[str, Any]],
    *,
    tokens_before: int,
    tokens_after: int,
    summary_text: str | None = None,
    summary_message: Mapping[str, Any] | None = None,
    marker_message: Mapping[str, Any] | None = None,
) -> CompactResult:
    """Bind method/summary/recent_count to the messages actually returned."""
    kept = _has_identity(messages, summary_message)
    return CompactResult(
        messages,
        True,
        "summary" if kept else "sliding_window",
        tokens_before,
        tokens_after,
        summary=summary_text if kept else None,
        recent_count=_recent_count_from_final(
            messages,
            summary_message,
            marker_message,
        ),
    )


def _sliding_window_apply(
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    keep_recent_turns: int = 4,
    protected: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    """Hard-truncate older turns. Third value is the produced marker, if any."""
    if not messages:
        return [], 0, None
    tokens_before = estimate_openai_tokens(messages)
    if tokens_before <= max_tokens:
        return [_retain_or_copy(m, protected) for m in messages], 0, None

    system, turns = _split_turns(messages, protected)
    if not turns:
        return [_retain_or_copy(m, protected) for m in messages], 0, None

    min_keep = max(1, keep_recent_turns)
    kept = turns[-min_keep:] if len(turns) > min_keep else list(turns)
    older = turns[:-min_keep] if len(turns) > min_keep else []
    for turn in reversed(older):
        candidate = system + _flatten_turns([turn] + kept)
        if estimate_openai_tokens(candidate) <= max_tokens:
            kept = [turn] + kept
        else:
            break

    kept_messages = _flatten_turns(kept)
    dropped = len(_flatten_turns(turns)) - len(kept_messages)
    marker: dict[str, Any] | None = None
    if dropped <= 0:
        result = system + kept_messages
    elif any(_is_identity_protected(m, protected) for m in system):
        # A Router-produced summary is already in the prefix; don't add a marker.
        result = system + kept_messages
    else:
        marker = _new_sliding_marker()
        result = system + [marker] + kept_messages
    tokens_after = estimate_openai_tokens(result)
    return result, max(0, tokens_before - tokens_after), marker


def sliding_window_openai(
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    keep_recent_turns: int = 4,
    protected: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], int]:
    """Hard-truncate older turns; keep leading systems and ``protected`` objects."""
    result, dropped, _marker = _sliding_window_apply(
        messages,
        max_tokens,
        keep_recent_turns=keep_recent_turns,
        protected=protected,
    )
    return result, dropped


def format_transcript(messages: Sequence[Mapping[str, Any]], *, max_chars: int = 24_000) -> str:
    lines: list[str] = []
    budget = max_chars
    for msg in messages:
        role = str(msg.get("role") or "user").upper()
        text = flatten_openai_content(msg.get("content")).strip()
        if not text:
            continue
        if len(text) > 2_000:
            text = text[:2_000] + "…"
        chunk = f"{role}: {text}"
        if len(chunk) > budget:
            if budget < 80:
                break
            chunk = chunk[:budget]
        lines.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(lines)


async def auto_compact_openai_messages(
    messages: list[dict[str, Any]],
    *,
    context_window: int,
    max_output_tokens: int,
    summarizer: Summarizer | None = None,
    keep_recent_turns: int = 4,
    force: bool = False,
    tokens_before: int | None = None,
) -> CompactResult:
    """Strip old images, then summarize or truncate at the compact threshold.

    ``tokens_before`` overrides the heuristic when the caller already has a
    model ``/tokenize`` count (threshold uses that true value).
    """
    tokens_before = (
        tokens_before if tokens_before is not None else estimate_openai_tokens(messages)
    )
    keep_n = max(1, keep_recent_turns)
    stripped, tokens_saved = strip_images_openai(messages, keep_recent_turns=keep_n)
    tokens_stripped = estimate_openai_tokens(stripped)

    if not force and not should_compact(context_window, tokens_before, max_output_tokens=max_output_tokens):
        outbound = list(messages)
        return CompactResult(
            outbound,
            False,
            "none",
            tokens_before,
            tokens_before,
            recent_count=_recent_count_from_final(outbound),
        )

    if (
        tokens_saved > 0
        and not force
        and not should_compact(context_window, tokens_stripped, max_output_tokens=max_output_tokens)
    ):
        return CompactResult(
            stripped,
            True,
            "strip_images",
            tokens_before,
            tokens_stripped,
            recent_count=_recent_count_from_final(stripped),
        )

    working = stripped if tokens_saved > 0 else [dict(m) for m in messages]
    threshold = get_auto_compact_threshold(context_window, max_output_tokens)
    system, turns = _split_turns(working)
    if len(turns) <= 1:
        if tokens_saved > 0:
            return CompactResult(
                stripped,
                True,
                "strip_images",
                tokens_before,
                tokens_stripped,
                recent_count=_recent_count_from_final(stripped),
            )
        outbound = list(messages)
        return CompactResult(
            outbound,
            False,
            "none",
            tokens_before,
            tokens_before,
            recent_count=_recent_count_from_final(outbound),
        )

    recent = _flatten_turns(turns[-keep_n:]) if len(turns) > keep_n else _flatten_turns(turns[-1:])
    old = _flatten_turns(turns[:-keep_n] if len(turns) > keep_n else turns[:-1])
    if not old:
        if tokens_saved > 0:
            return CompactResult(
                stripped,
                True,
                "strip_images",
                tokens_before,
                tokens_stripped,
                recent_count=_recent_count_from_final(stripped),
            )
        outbound = list(messages)
        return CompactResult(
            outbound,
            False,
            "none",
            tokens_before,
            tokens_before,
            recent_count=_recent_count_from_final(outbound),
        )

    if summarizer is not None:
        try:
            summary = await summarizer(old)
        except Exception:
            summary = None
        if isinstance(summary, str) and summary.strip():
            summary_text = summary.strip()
            summary_msg = {
                "role": "user",
                "content": f"{HISTORY_SUMMARY_PREFIX}\n{summary_text}",
            }
            compacted = system + [summary_msg] + recent
            after = estimate_openai_tokens(compacted)
            marker_message: dict[str, Any] | None = None
            if after <= tokens_before:
                if not force and after > threshold:
                    compacted, _, marker_message = _sliding_window_apply(
                        compacted,
                        threshold,
                        keep_recent_turns=max(1, keep_recent_turns - 1),
                        protected=(summary_msg,),
                    )
                    after = estimate_openai_tokens(compacted)
                return _result_from_outbound(
                    compacted,
                    tokens_before=tokens_before,
                    tokens_after=after,
                    summary_text=summary_text,
                    summary_message=summary_msg,
                    marker_message=marker_message,
                )

    if force:
        marker_message = _new_sliding_marker()
        compacted = system + [marker_message] + recent
        after = estimate_openai_tokens(compacted)
        return _result_from_outbound(
            compacted,
            tokens_before=tokens_before,
            tokens_after=after,
            marker_message=marker_message,
        )

    compacted, _, marker_message = _sliding_window_apply(
        working, threshold, keep_recent_turns=keep_n
    )
    after = estimate_openai_tokens(compacted)
    if after >= tokens_before:
        if tokens_saved > 0:
            return CompactResult(
                stripped,
                True,
                "strip_images",
                tokens_before,
                tokens_stripped,
                recent_count=_recent_count_from_final(stripped),
            )
        outbound = list(messages)
        return CompactResult(
            outbound,
            False,
            "none",
            tokens_before,
            tokens_before,
            recent_count=_recent_count_from_final(outbound),
        )
    return _result_from_outbound(
        compacted,
        tokens_before=tokens_before,
        tokens_after=after,
        marker_message=marker_message,
    )
