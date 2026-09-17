"""Unit tests for call-side LLM guards and sampling defaults."""

from __future__ import annotations

import logging

import pytest

from anila_core.models.message import AssistantMessage, UserMessage
from anila_core.prompts.sampling import TASK_SAMPLING, get_sampling
from anila_core.providers.guards import (
    bumped_max_tokens,
    is_empty_length_failure,
    is_empty_reply,
)
from anila_core.providers.mock import MockProvider, ScriptedResponse


# ---------------------------------------------------------------------------
# is_empty_length_failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("finish_reason", "content", "expected"),
    [
        ("length", "", True),
        ("length", None, True),
        ("length", "   \n\t  ", True),
        ("length", "有內容", False),
        ("stop", "", False),
        ("stop", None, False),
        ("end_turn", "", False),
        (None, "", False),
        ("", "", False),
        ("length", "0", False),
    ],
)
def test_is_empty_length_failure_truth_table(
    finish_reason: str | None, content: str | None, expected: bool
) -> None:
    assert is_empty_length_failure(finish_reason, content) is expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", True),
        (None, True),
        ("   \n\t  ", True),
        ("有內容", False),
        ("0", False),
    ],
)
def test_is_empty_reply_truth_table(content: str | None, expected: bool) -> None:
    assert is_empty_reply(content) is expected


# ---------------------------------------------------------------------------
# bumped_max_tokens
# ---------------------------------------------------------------------------


def test_bumped_max_tokens_doubles() -> None:
    assert bumped_max_tokens(1024) == 2048
    assert bumped_max_tokens(2048) == 4096


def test_bumped_max_tokens_respects_cap() -> None:
    assert bumped_max_tokens(5000, cap=8192) == 8192
    assert bumped_max_tokens(8192, cap=8192) == 8192
    assert bumped_max_tokens(100, cap=150) == 150


def test_bumped_max_tokens_never_lowers_above_cap() -> None:
    """Invariant 5: bumped_max_tokens(x) >= x for all x >= 0; x > cap case."""
    assert bumped_max_tokens(10000) == 20000
    assert bumped_max_tokens(20000) == 32768
    assert bumped_max_tokens(32768) == 32768
    assert bumped_max_tokens(40000) == 40000
    assert bumped_max_tokens(40000) >= 40000
    assert bumped_max_tokens(9000, cap=8192) == 9000
    assert bumped_max_tokens(0) == 1
    assert bumped_max_tokens(1) == 2


# ---------------------------------------------------------------------------
# sampling table
# ---------------------------------------------------------------------------


def test_task_sampling_table_entries() -> None:
    assert get_sampling("rag_qa") == TASK_SAMPLING["rag_qa"]
    assert get_sampling("chips").max_tokens == 1024
    assert get_sampling("chips").temperature == 0.4
    assert get_sampling("chat").max_tokens == 2048
    assert get_sampling("json_gen").temperature == 0.15
    assert get_sampling("title").temperature == 0.3


def test_get_sampling_unknown_task() -> None:
    with pytest.raises(KeyError, match="未知取樣任務"):
        get_sampling("no-such-task")


def test_sampling_docstring_marks_unwired_tasks() -> None:
    """Invariant / F6: table must not claim all five tasks are wired."""
    from anila_core.prompts import sampling as sampling_mod

    doc = sampling_mod.__doc__ or ""
    assert "已接線" in doc
    assert "尚未接線" in doc
    assert "chips" in doc
