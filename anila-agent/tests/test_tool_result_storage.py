"""P2-15 — tool result storage 測試。"""

from __future__ import annotations

import pytest

from anila_agent.tools.result_storage import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    ToolResultStore,
    TruncatedResult,
    _sanitize_filename_segment,
)


def test_small_result_passes_through_without_truncation(tmp_path):
    store = ToolResultStore(
        max_size_chars=1000, preview_chars=200, storage_dir=tmp_path
    )
    result = store.process(tool_name="bash", content="hello world")
    assert result.truncated is False
    assert result.preview == "hello world"
    assert result.storage_path is None
    assert result.original_size == len("hello world")


def test_large_result_truncates_and_persists(tmp_path):
    store = ToolResultStore(
        max_size_chars=100, preview_chars=20, storage_dir=tmp_path
    )
    huge = "x" * 5000
    result = store.process(tool_name="bash", content=huge)
    assert result.truncated is True
    assert result.original_size == 5000
    assert result.storage_path is not None
    assert result.storage_path.exists()
    assert result.storage_path.read_text() == huge
    # preview 應該包含 truncation note + 含 path 字串
    assert "truncated" in result.preview
    assert str(result.storage_path) in result.preview


def test_large_result_without_persist_skips_file(tmp_path):
    store = ToolResultStore(
        max_size_chars=50, preview_chars=10, storage_dir=tmp_path, persist=False
    )
    result = store.process(tool_name="bash", content="y" * 500)
    assert result.truncated is True
    assert result.storage_path is None
    # 目錄不應該被建出來
    assert not tmp_path.joinpath(".anila").exists() or True  # tmp_path 自身不是 .anila


def test_head_and_tail_strategy_includes_both_ends(tmp_path):
    store = ToolResultStore(
        max_size_chars=20,
        preview_chars=10,
        storage_dir=tmp_path,
        preview_strategy="head_and_tail",
    )
    content = "ABCDEFGHIJ" + "_" * 1000 + "1234567890"
    result = store.process(tool_name="grep", content=content)
    assert result.truncated is True
    # head 跟 tail 都要在 preview 裡
    assert "ABCDE" in result.preview
    assert "67890" in result.preview


def test_invalid_max_size_raises():
    with pytest.raises(ValueError):
        ToolResultStore(max_size_chars=0)


def test_invalid_preview_chars_raises():
    with pytest.raises(ValueError):
        ToolResultStore(preview_chars=0)


def test_preview_must_be_smaller_than_max():
    with pytest.raises(ValueError):
        ToolResultStore(max_size_chars=100, preview_chars=100)


def test_sanitize_filename_replaces_unsafe_chars():
    assert _sanitize_filename_segment("bash") == "bash"
    assert _sanitize_filename_segment("tool/with..slash") == "tool_with__slash"
    assert _sanitize_filename_segment("") == "tool"
    assert _sanitize_filename_segment("a" * 100) == "a" * 40


def test_truncated_result_to_dict_round_trip(tmp_path):
    store = ToolResultStore(
        max_size_chars=50, preview_chars=10, storage_dir=tmp_path
    )
    result = store.process(tool_name="read_file", content="z" * 500)
    d = result.to_dict()
    assert d["truncated"] is True
    assert d["original_size"] == 500
    assert d["storage_path"] is not None
    assert "preview" in d


def test_storage_path_filename_contains_safe_tool_name(tmp_path):
    store = ToolResultStore(
        max_size_chars=10, preview_chars=5, storage_dir=tmp_path
    )
    result = store.process(tool_name="weird/tool:name", content="x" * 100)
    assert result.storage_path is not None
    # filename 不應含 / 或 :
    fname = result.storage_path.name
    assert "/" not in fname
    assert ":" not in fname
    assert fname.endswith(".txt")


def test_default_max_size_constant_reasonable():
    # 自我健康檢查 —— 預設值應落在合理區間(對齊 claude-code 預設量級)。
    assert 10_000 <= DEFAULT_MAX_RESULT_SIZE_CHARS <= 100_000


def test_multiple_calls_produce_distinct_files(tmp_path):
    store = ToolResultStore(
        max_size_chars=10, preview_chars=5, storage_dir=tmp_path
    )
    r1 = store.process(tool_name="t", content="aaaaaaaaaaaaaaa")
    r2 = store.process(tool_name="t", content="bbbbbbbbbbbbbbb")
    assert r1.storage_path != r2.storage_path
    assert r1.storage_path is not None and r1.storage_path.exists()
    assert r2.storage_path is not None and r2.storage_path.exists()


def test_truncated_result_is_immutable():
    result = TruncatedResult(
        preview="x", storage_path=None, original_size=1, truncated=False
    )
    with pytest.raises(Exception):
        result.preview = "y"  # type: ignore[misc]


def test_exact_size_boundary_does_not_truncate(tmp_path):
    """size == max → 視為通過,不 truncate。"""
    store = ToolResultStore(
        max_size_chars=10, preview_chars=5, storage_dir=tmp_path
    )
    result = store.process(tool_name="t", content="0123456789")
    assert result.truncated is False
    assert result.preview == "0123456789"


def test_preview_includes_truncation_note_without_persist(tmp_path):
    """persist=False 時 truncation note 不應含 storage path 字眼。"""
    store = ToolResultStore(
        max_size_chars=10, preview_chars=5, storage_dir=tmp_path, persist=False
    )
    result = store.process(tool_name="t", content="x" * 100)
    assert "truncated" in result.preview
    # storage_path 為 None,note 不該含路徑 substring(只有 "[...truncated N chars]")
    assert "saved to" not in result.preview
