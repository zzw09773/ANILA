"""deep_research：context 組裝（純函式）。"""

from __future__ import annotations

import pytest

from anila_agent.orchestration.deep_research import _format_context
from anila_agent.retrieval.schemas import Document

pytestmark = pytest.mark.unit


def test_format_context_includes_query_subqueries_and_ids():
    blocks = [
        ("子問題甲", [Document(id="d1", text="內容一"), Document(id="d2", text="內容二")]),
        ("子問題乙", []),
    ]
    out = _format_context("原問題", blocks)
    assert "原問題" in out
    assert "子問題甲" in out and "子問題乙" in out
    assert "[d1] 內容一" in out and "[d2] 內容二" in out
    assert "（無檢索結果）" in out  # 空結果標示
