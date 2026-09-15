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

HISTORY_SUMMARY_PREFIX = "[歷史摘要]"

Summarizer = Callable[[list[dict[str, Any]]], Awaitable[str | None]]


@dataclass(frozen=True)
class CompactResult:
    messages: list[dict[str, Any]]
    compacted: bool
    method: str  # "none" | "summary" | "sliding_window"
    tokens_before: int
    tokens_after: int


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
    total = 0
    for msg in messages:
        total += len(flatten_openai_content(msg.get("content")))
        if isinstance(msg, Mapping):
            reasoning = msg.get("reasoning") or msg.get("reasoning_content")
            if isinstance(reasoning, str):
                total += len(reasoning)
    return int((total / 4) * (4 / 3))


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


def _is_tool_result(msg: Mapping[str, Any]) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _split_turns(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    system: list[dict[str, Any]] = []
    convo: list[dict[str, Any]] = []
    for msg in messages:
        if _is_system(msg):
            system.append(dict(msg))
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


def sliding_window_openai(
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    keep_recent_turns: int = 4,
) -> tuple[list[dict[str, Any]], int]:
    """Hard-truncate older turns; keep the leading system message."""
    if not messages:
        return [], 0
    tokens_before = estimate_openai_tokens(messages)
    if tokens_before <= max_tokens:
        return [dict(m) for m in messages], 0

    system, turns = _split_turns(messages)
    if not turns:
        return [dict(m) for m in messages], 0

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
    if dropped <= 0:
        result = system + kept_messages
    else:
        result = system + [{"role": "user", "content": SLIDING_WINDOW_SUMMARY}] + kept_messages
    tokens_after = estimate_openai_tokens(result)
    return result, max(0, tokens_before - tokens_after)


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
) -> CompactResult:
    """Summarize or truncate when the history is at the compact threshold."""
    tokens_before = estimate_openai_tokens(messages)
    if not force and not should_compact(context_window, tokens_before, max_output_tokens=max_output_tokens):
        return CompactResult(list(messages), False, "none", tokens_before, tokens_before)

    threshold = get_auto_compact_threshold(context_window, max_output_tokens)
    system, turns = _split_turns(messages)
    if len(turns) <= 1:
        return CompactResult(list(messages), False, "none", tokens_before, tokens_before)

    keep_n = max(1, keep_recent_turns)
    recent = _flatten_turns(turns[-keep_n:]) if len(turns) > keep_n else _flatten_turns(turns[-1:])
    old = _flatten_turns(turns[:-keep_n] if len(turns) > keep_n else turns[:-1])
    if not old:
        return CompactResult(list(messages), False, "none", tokens_before, tokens_before)

    if summarizer is not None:
        try:
            summary = await summarizer(old)
        except Exception:
            summary = None
        if isinstance(summary, str) and summary.strip():
            compacted = system + [
                {"role": "user", "content": f"{HISTORY_SUMMARY_PREFIX}\n{summary.strip()}"}
            ] + recent
            after = estimate_openai_tokens(compacted)
            if after <= tokens_before:
                if not force and after > threshold:
                    compacted, _ = sliding_window_openai(
                        compacted, threshold, keep_recent_turns=max(1, keep_recent_turns - 1)
                    )
                    after = estimate_openai_tokens(compacted)
                return CompactResult(compacted, True, "summary", tokens_before, after)

    if force:
        compacted = system + [{"role": "user", "content": SLIDING_WINDOW_SUMMARY}] + recent
        after = estimate_openai_tokens(compacted)
        return CompactResult(compacted, True, "sliding_window", tokens_before, after)

    compacted, _ = sliding_window_openai(messages, threshold, keep_recent_turns=keep_n)
    after = estimate_openai_tokens(compacted)
    if compacted == messages or after >= tokens_before:
        return CompactResult(list(messages), False, "none", tokens_before, tokens_before)
    return CompactResult(compacted, True, "sliding_window", tokens_before, after)
