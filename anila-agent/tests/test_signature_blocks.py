"""P2-15 — stripSignatureBlocks 測試。"""

from __future__ import annotations

from anila_agent.memory.signature_blocks import (
    has_signature,
    is_droppable_block,
    strip_signature_blocks,
    strip_signature_from_block,
)


def test_strip_top_level_signature_field():
    msg = {"role": "assistant", "content": "ok", "signature": "abcd1234"}
    cleaned = strip_signature_blocks([msg])
    assert cleaned[0] == {"role": "assistant", "content": "ok"}
    # 不 mutate 輸入
    assert "signature" in msg


def test_strip_signature_in_content_blocks():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "thinking", "thinking": "step 1", "signature": "xyz"},
        ],
    }
    cleaned = strip_signature_blocks([msg])
    blocks = cleaned[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Hello"}
    assert blocks[1] == {"type": "thinking", "thinking": "step 1"}
    assert "signature" not in blocks[1]


def test_drop_redacted_thinking_block():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "redacted_thinking", "data": "encrypted-blob"},
        ],
    }
    cleaned = strip_signature_blocks([msg])
    blocks = cleaned[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"


def test_string_content_passthrough():
    msg = {"role": "user", "content": "plain text"}
    cleaned = strip_signature_blocks([msg])
    assert cleaned[0] == msg
    # 同樣不 mutate;不過 dict 是新的
    assert cleaned[0] is not msg


def test_none_content_passthrough():
    msg = {"role": "assistant", "content": None, "tool_call_id": "x"}
    cleaned = strip_signature_blocks([msg])
    assert cleaned[0]["content"] is None
    assert cleaned[0]["tool_call_id"] == "x"


def test_nested_signature_in_dict_value_is_removed():
    block = {
        "type": "tool_use",
        "metadata": {"signature": "nested", "model": "gemma"},
    }
    cleaned = strip_signature_from_block(block)
    assert "signature" not in cleaned["metadata"]
    assert cleaned["metadata"]["model"] == "gemma"


def test_nested_signature_in_list_values_removed():
    block = {
        "type": "compound",
        "inner_blocks": [
            {"type": "text", "text": "ok"},
            {"type": "thinking", "signature": "z", "thinking": "step"},
        ],
    }
    cleaned = strip_signature_from_block(block)
    assert "signature" not in cleaned["inner_blocks"][1]
    assert cleaned["inner_blocks"][1]["thinking"] == "step"


def test_is_droppable_block_recognises_redacted_thinking():
    assert is_droppable_block({"type": "redacted_thinking"}) is True
    assert is_droppable_block({"type": "text"}) is False
    assert is_droppable_block("not a dict") is False  # type: ignore[arg-type]
    assert is_droppable_block(None) is False  # type: ignore[arg-type]


def test_has_signature_detects_top_level():
    msgs = [{"role": "assistant", "content": "x", "signature": "s"}]
    assert has_signature(msgs) is True


def test_has_signature_detects_in_content_block():
    msgs = [
        {
            "role": "assistant",
            "content": [{"type": "thinking", "signature": "z"}],
        }
    ]
    assert has_signature(msgs) is True


def test_has_signature_detects_redacted_block():
    msgs = [
        {"role": "assistant", "content": [{"type": "redacted_thinking"}]}
    ]
    assert has_signature(msgs) is True


def test_has_signature_false_for_clean_messages():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
    ]
    assert has_signature(msgs) is False


def test_strip_idempotent():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "a"},
                {"type": "thinking", "signature": "s", "thinking": "t"},
                {"type": "redacted_thinking", "data": "x"},
            ],
            "signature": "outer",
        }
    ]
    once = strip_signature_blocks(msgs)
    twice = strip_signature_blocks(once)
    assert once == twice
    assert has_signature(once) is False


def test_empty_list_returns_empty_list():
    assert strip_signature_blocks([]) == []


def test_non_dict_in_content_passes_through():
    # content 內混入非 dict element(例如純字串 block)— 應該保留。
    msg = {"role": "assistant", "content": ["plain string", {"type": "text", "text": "x"}]}
    cleaned = strip_signature_blocks([msg])
    assert cleaned[0]["content"][0] == "plain string"
    assert cleaned[0]["content"][1] == {"type": "text", "text": "x"}
