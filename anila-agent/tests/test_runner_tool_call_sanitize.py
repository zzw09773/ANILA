"""Runner strips gemma-native tool-call tokens that leak into final_output (#114).

The fixture strings below were captured live from gemma-4-31B-it (vLLM) by
issuing a chat-completion whose tool was described in the prompt but NOT passed
as a structured ``tools`` array — which is the shape that surfaces gemma's raw
native tool-call format. That raw block is exactly what leaks into the assistant
content (and thus AnilaRunner final_output) when the serving stack's tool-call
parser misses it. AnilaRunner.send() applies _strip_leaked_tool_calls to any
string final_output.
"""
from __future__ import annotations

import pytest

from anila_agent.core.runner import _strip_leaked_tool_calls

# Real captures (verbatim from the model):
_SINGLE = "<|tool_call>call:get_weather{city: '台北'}<tool_call|>"
_MULTI = (
    "<|tool_call>call:get_weather{city: '東京'}<tool_call|>"
    "<|tool_call>call:get_weather{city: '大阪'}<tool_call|>"
)


def test_strips_single_real_block():
    cleaned, n = _strip_leaked_tool_calls(_SINGLE)
    assert n == 1
    assert cleaned == ""  # content was only the leaked call


def test_strips_multiple_real_blocks():
    cleaned, n = _strip_leaked_tool_calls(_MULTI)
    assert n == 2
    assert cleaned == ""


def test_keeps_surrounding_prose():
    text = f"好的,我來查詢。{_SINGLE} 請稍候。"
    cleaned, n = _strip_leaked_tool_calls(text)
    assert n == 1
    assert "好的,我來查詢。" in cleaned
    assert "請稍候。" in cleaned
    assert "tool_call" not in cleaned


def test_strips_truncated_block_without_close():
    # max_tokens can cut the block off before the closing token.
    truncated = "<|tool_call>call:get_weather{city: '台北"
    cleaned, n = _strip_leaked_tool_calls(truncated)
    assert n == 1
    assert cleaned == ""


def test_multiline_args_are_covered():
    text = "<|tool_call>call:search{\n  query: 'multi\nline'\n}<tool_call|>"
    cleaned, n = _strip_leaked_tool_calls(text)
    assert n == 1
    assert cleaned == ""


def test_clean_output_unchanged():
    text = "台北今天多雲,氣溫 24°C。"
    cleaned, n = _strip_leaked_tool_calls(text)
    assert n == 0
    assert cleaned == text


def test_empty_string():
    assert _strip_leaked_tool_calls("") == ("", 0)


@pytest.mark.parametrize("text", [_SINGLE, _MULTI])
def test_no_token_remains_after_strip(text):
    cleaned, _ = _strip_leaked_tool_calls(text)
    assert "<|tool_call>" not in cleaned
    assert "<tool_call|>" not in cleaned
