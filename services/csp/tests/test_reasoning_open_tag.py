"""Bounded unfinished think-tag state across arbitrary token boundaries."""
from __future__ import annotations

import asyncio

import pytest

from app.services.internal_llm import InternalCompletionError
from app.services.reasoning_gate import (
    MAX_PENDING_THINK_TAG_CHARS, ContentRedactor, ReasoningTagLimitError,
    redact_visible_text,
)
from tests.test_quick_model_stream import collect, frame, gateway as _gateway

gateway = _gateway


def visible(parts):
    redactor = ContentRedactor(reveal=False)
    output = []
    for part in parts:
        text, _ = redactor.push(part)
        assert "PRIVATE_ANALYSIS" not in text
        output.append(text)
    tail, _ = redactor.finish()
    assert "PRIVATE_ANALYSIS" not in tail
    output.append(tail)
    return "".join(output)


@pytest.mark.parametrize("opening", ["<think " + " " * 30 + ">", "<thinking " + " " * 500 + ">",
                                     '<ThInK data-value="synthetic attribute">'])
def test_every_split_and_character_partition_matches_whole(opening):
    raw = "前文。" + opening + "PRIVATE_ANALYSIS</think>公開回答"
    expected = redact_visible_text(raw, reveal=False)
    assert expected == "前文。公開回答"
    for cut in range(len(raw) + 1):
        assert visible([raw[:cut], raw[cut:]]) == expected
    assert visible(list(raw)) == expected


@pytest.mark.parametrize("raw", ["<think " + " " * 30 + "PRIVATE_ANALYSIS",
                                 "<thinking attr=PRIVATE_ANALYSIS",
                                 "<think>PRIVATE_ANALYSIS", "<thinking>PRIVATE_ANALYSIS</thi"])
def test_unclosed_private_tags_never_flush_private_tail(raw):
    assert visible([raw]) == ""
    assert visible(list(raw)) == ""
    for cut in range(len(raw) + 1):
        assert visible([raw[:cut], raw[cut:]]) == ""


@pytest.mark.parametrize("partition", ["whole", "each"])
def test_oversized_open_tag_fails_closed_with_bounded_pending(partition):
    raw = "<think " + " " * MAX_PENDING_THINK_TAG_CHARS + ">PRIVATE_ANALYSIS</think>公開回答"
    redactor = ContentRedactor(reveal=False)
    output = []
    with pytest.raises(ReasoningTagLimitError):
        for part in [raw] if partition == "whole" else raw:
            text, _ = redactor.push(part)
            output.append(text)
    assert "PRIVATE_ANALYSIS" not in "".join(output)
    assert redactor._buf == redactor._hold == ""
    assert redactor.push("PRIVATE_ANALYSIS") == ("", [])
    assert redactor.finish() == ("", [])


def test_adapter_open_tag_limit_closes_and_accounts_partial_once(db, gateway):
    gateway.chunks = [frame("安全前文。"), frame("<think " + " " * MAX_PENDING_THINK_TAG_CHARS),
                      frame(">PRIVATE_ANALYSIS</think>公開回答"), "data: [DONE]\n\n"]
    seen = []

    async def receive(text):
        seen.append(text)

    with pytest.raises(InternalCompletionError):
        asyncio.run(collect(db, gateway, on_delta=receive))
    assert "".join(seen) == "安全前文。"
    assert gateway.normal == [] and len(gateway.partial) == 1
    assert gateway.partial[0]["outcome"] == "partial"
    assert gateway.closed == 1


def test_cancel_while_open_tag_is_pending_never_flushes_private_tail(db, gateway):
    gateway.chunks = [frame("安全前文。"), frame("<think " + " " * 30 + "PRIVATE_ANALYSIS")]
    gateway.park = True
    pending = asyncio.Event()
    seen = []
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        # Initial guard + first visible content. The HTTP body subsequently
        # parks with the private open tag entirely within redactor state.
        if calls == 2:
            pending.set()

    async def receive(text):
        seen.append(text)

    async def scenario():
        task = asyncio.create_task(collect(db, gateway, before_chunk=guard, on_delta=receive))
        await pending.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert "".join(seen) == "安全前文。"
    assert gateway.closed == 1
    assert gateway.normal == [] and len(gateway.partial) == 1
