"""P2-15 — autoCompactBoundary 測試。"""

from __future__ import annotations

import pytest

from anila_agent.memory.compact_boundary import (
    BOUNDARY_CONTENT_PREFIX,
    BOUNDARY_TYPE,
    AutoCompactBoundary,
    extract_boundaries,
    insert_boundary,
    is_boundary_message,
    split_by_boundaries,
    strip_boundaries,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_boundary_to_message_has_required_fields():
    boundary = AutoCompactBoundary(
        compactor_name="micro",
        before_messages=20,
        after_messages=8,
        before_tokens=8000,
        after_tokens=3000,
    )
    msg = boundary.to_message()
    assert msg["type"] == BOUNDARY_TYPE
    assert msg["role"] == "system"
    assert msg["content"].startswith(BOUNDARY_CONTENT_PREFIX)
    assert msg["meta"]["compactor_name"] == "micro"
    assert msg["meta"]["before_messages"] == 20
    assert msg["meta"]["after_messages"] == 8


def test_boundary_id_unique_across_instances():
    b1 = AutoCompactBoundary()
    b2 = AutoCompactBoundary()
    assert b1.boundary_id != b2.boundary_id
    assert len(b1.boundary_id) == 32  # uuid hex


def test_is_boundary_message_detects_by_type():
    boundary = AutoCompactBoundary(compactor_name="snip")
    assert is_boundary_message(boundary.to_message()) is True
    assert is_boundary_message(_msg("user", "hello")) is False


def test_is_boundary_message_detects_by_content_prefix_fallback():
    # type 欄位被某個 wire format 丟掉時,fallback 看 content prefix。
    raw = {"role": "system", "content": f"{BOUNDARY_CONTENT_PREFIX} stale"}
    assert is_boundary_message(raw) is True


def test_is_boundary_message_rejects_non_dict():
    assert is_boundary_message("not a dict") is False  # type: ignore[arg-type]
    assert is_boundary_message(None) is False  # type: ignore[arg-type]


def test_insert_boundary_appends_and_does_not_mutate():
    original = [_msg("user", "a"), _msg("assistant", "b")]
    boundary = AutoCompactBoundary(compactor_name="micro")
    new_list = insert_boundary(original, boundary)
    assert len(new_list) == 3
    assert is_boundary_message(new_list[-1])
    # original 不應該被改
    assert len(original) == 2


def test_extract_boundaries_keeps_order():
    b1 = AutoCompactBoundary(compactor_name="micro")
    b2 = AutoCompactBoundary(compactor_name="snip")
    messages = [
        _msg("user", "a"),
        b1.to_message(),
        _msg("assistant", "b"),
        b2.to_message(),
        _msg("user", "c"),
    ]
    boundaries = extract_boundaries(messages)
    assert len(boundaries) == 2
    assert boundaries[0]["meta"]["compactor_name"] == "micro"
    assert boundaries[1]["meta"]["compactor_name"] == "snip"


def test_strip_boundaries_removes_only_markers():
    b = AutoCompactBoundary(compactor_name="micro")
    messages = [
        _msg("user", "a"),
        b.to_message(),
        _msg("assistant", "b"),
    ]
    cleaned = strip_boundaries(messages)
    assert len(cleaned) == 2
    assert all(not is_boundary_message(m) for m in cleaned)


def test_split_by_boundaries_creates_chunks():
    b1 = AutoCompactBoundary(compactor_name="micro")
    b2 = AutoCompactBoundary(compactor_name="snip")
    messages = [
        _msg("user", "a"),
        _msg("assistant", "b"),
        b1.to_message(),
        _msg("user", "c"),
        b2.to_message(),
        _msg("assistant", "d"),
    ]
    chunks = split_by_boundaries(messages)
    assert len(chunks) == 3
    assert chunks[0] == [_msg("user", "a"), _msg("assistant", "b")]
    assert chunks[1] == [_msg("user", "c")]
    assert chunks[2] == [_msg("assistant", "d")]


def test_split_by_boundaries_no_marker_returns_single_chunk():
    messages = [_msg("user", "a"), _msg("assistant", "b")]
    chunks = split_by_boundaries(messages)
    assert len(chunks) == 1
    assert chunks[0] == messages


def test_split_by_boundaries_with_leading_boundary_keeps_empty_chunk():
    b = AutoCompactBoundary(compactor_name="micro")
    messages = [b.to_message(), _msg("user", "a")]
    chunks = split_by_boundaries(messages)
    assert len(chunks) == 2
    assert chunks[0] == []
    assert chunks[1] == [_msg("user", "a")]


def test_boundary_extra_metadata_propagates():
    boundary = AutoCompactBoundary(
        compactor_name="micro",
        extra={"chain_id": "abc", "trace_id": "xyz"},
    )
    meta = boundary.to_message()["meta"]
    assert meta["chain_id"] == "abc"
    assert meta["trace_id"] == "xyz"


def test_boundary_dataclass_is_frozen():
    boundary = AutoCompactBoundary()
    with pytest.raises(Exception):
        boundary.compactor_name = "x"  # type: ignore[misc]


def test_to_message_timestamp_is_iso_string():
    boundary = AutoCompactBoundary()
    msg = boundary.to_message()
    ts = msg["meta"]["timestamp"]
    assert isinstance(ts, str)
    # 寬鬆檢驗 ISO 格式
    assert "T" in ts
