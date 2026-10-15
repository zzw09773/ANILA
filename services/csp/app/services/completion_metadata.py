"""Bounded, provider-independent model completion metadata for Quick flows."""
from __future__ import annotations

import math

FINISH_REASONS = frozenset(
    {"stop", "length", "content_filter", "tool_calls", "function_call"}
)
USAGE_FIELDS = frozenset(
    {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"}
)
_WARNINGS = {
    "length": (
        "MODEL_OUTPUT_LIMIT",
        "回答已達輸出長度上限，內容尚未完成。請縮小問題或接續提問（MODEL_OUTPUT_LIMIT）",
    ),
    "content_filter": (
        "MODEL_CONTENT_FILTER",
        "回覆因內容限制未完整提供，請調整問題後再試（MODEL_CONTENT_FILTER）",
    ),
    "tool_calls": (
        "MODEL_ACTION_REQUIRED",
        "模型要求工具處理，這次回答尚未完成。請聯絡助手建立者（MODEL_ACTION_REQUIRED）",
    ),
}
_WARNINGS["function_call"] = _WARNINGS["tool_calls"]
_META_FIELDS = frozenset(
    {
        "schema_version", "finish_reason", "terminal_source", "answer_complete",
        "usage_complete", "can_continue", "usage",
    }
)


class CompletionMetadataError(ValueError):
    def __init__(self):
        super().__init__("模型完成資訊格式不正確（MODEL_COMPLETION_METADATA）")


def completion_metadata(
    *,
    finish_reason: str | None = None,
    terminal_source: str = "response",
    usage: object = None,
    usage_complete: bool = True,
    can_continue: bool = False,
) -> dict:
    """Rebuild a JSON-compatible allow-list; never carry provider messages."""
    if (
        finish_reason is not None
        and (type(finish_reason) is not str or finish_reason not in FINISH_REASONS)
    ):
        raise CompletionMetadataError()
    if (
        type(terminal_source) is not str
        or terminal_source not in {"finish_reason", "done", "response"}
        or (terminal_source == "finish_reason" and finish_reason is None)
        or (terminal_source == "done" and finish_reason is not None)
        or type(usage_complete) is not bool
        or type(can_continue) is not bool
        or (usage is not None and type(usage) is not dict)
    ):
        raise CompletionMetadataError()
    cleaned_usage = {}
    for key in USAGE_FIELDS:
        if usage is None or key not in usage:
            continue
        value = usage[key]
        if value is None:
            continue
        if (
            type(value) not in (int, float)
            or not 0 <= value <= 2**53 - 1
            or not math.isfinite(value)
            or (type(value) is float and not value.is_integer())
        ):
            raise CompletionMetadataError()
        cleaned_usage[key] = int(value)
    result = {
        "schema_version": 1,
        "finish_reason": finish_reason,
        "terminal_source": terminal_source,
        "answer_complete": finish_reason not in _WARNINGS,
        "usage_complete": usage_complete,
        "can_continue": can_continue,
        "usage": cleaned_usage,
    }
    if finish_reason in _WARNINGS:
        result["type"], result["message"] = _WARNINGS[finish_reason]
    return result


def normalize_completion(value: object) -> dict:
    """Normalize legacy text or the exact structured callback envelope.

    Provider type/message are discarded and reconstructed canonically. Other
    unknown fields, inconsistent completion flags and schema versions fail shut.
    """
    if type(value) is str:
        return {"text": value, "completion": completion_metadata()}
    if type(value) is not dict or set(value) != {"text", "completion"}:
        raise CompletionMetadataError()
    text, meta = value["text"], value["completion"]
    if (
        type(text) is not str
        or type(meta) is not dict
        or not _META_FIELDS <= set(meta)
        or set(meta) - _META_FIELDS - {"type", "message"}
        or type(meta["schema_version"]) is not int
        or meta["schema_version"] != 1
        or type(meta["answer_complete"]) is not bool
        or type(meta["usage"]) is not dict
    ):
        raise CompletionMetadataError()
    rebuilt = completion_metadata(
        finish_reason=meta["finish_reason"],
        terminal_source=meta["terminal_source"],
        usage=meta["usage"],
        usage_complete=meta["usage_complete"],
        can_continue=meta["can_continue"],
    )
    if meta["answer_complete"] != rebuilt["answer_complete"]:
        raise CompletionMetadataError()
    return {"text": text, "completion": rebuilt}
