"""呼叫端 LLM 守則（純函式，無 I/O）。

設計依據：``docs/designs/ncsist-prompt-localization-and-harness.md`` §9b-2
——思考型模型可能把 completion tokens 全燒在 reasoning，回傳
``finish_reason=length`` 且 content 全空；不可把空字串當「模型沒話說」。
"""

from __future__ import annotations


def is_empty_length_failure(
    finish_reason: str | None, content: str | None
) -> bool:
    """True when finish is ``length`` and content is empty/whitespace."""
    if finish_reason != "length":
        return False
    return not (content or "").strip()


# Thinking models + a long HTML/code page routinely exceed 8k. The empty-length
# retry used to clamp here and then tell the UI the LLM was down.
LENGTH_RETRY_CAP = 32768


def bumped_max_tokens(current: int, *, cap: int = LENGTH_RETRY_CAP) -> int:
    """Double ``current``, never lowering below ``current``.

    When ``current`` already exceeds ``cap``, return ``current`` unchanged
    so a retry cannot shrink the budget. Otherwise double and clamp to
    ``cap``.
    """
    if current <= 0:
        return min(1, cap) if cap > 0 else 0
    if current >= cap:
        return current
    return min(current * 2, cap)
