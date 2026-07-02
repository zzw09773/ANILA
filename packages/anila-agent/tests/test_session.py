"""短期 session：SummarizingSession 壓縮行為。"""

from __future__ import annotations

import pytest

from anila_agent.memory.session import SummarizingSession

pytestmark = pytest.mark.unit


class _FakeSession:
    def __init__(self):
        self.items: list[dict] = []

    async def get_items(self, limit=None):
        return self.items[-limit:] if limit else list(self.items)

    async def add_items(self, items):
        self.items.extend(items)

    async def pop_item(self):
        return self.items.pop() if self.items else None

    async def clear_session(self):
        self.items = []


async def _summ(head):
    return f"摘要({len(head)})"


async def test_no_compaction_below_threshold():
    inner = _FakeSession()
    s = SummarizingSession(inner, _summ, max_items=5, keep_last=2)
    await s.add_items([{"role": "user", "content": str(i)} for i in range(3)])
    items = await s.get_items()
    assert len(items) == 3
    assert all(it["role"] == "user" for it in items)  # 無摘要插入


async def test_compaction_replaces_old_with_summary():
    inner = _FakeSession()
    s = SummarizingSession(inner, _summ, max_items=5, keep_last=2)
    await s.add_items([{"role": "user", "content": str(i)} for i in range(6)])  # 6 > 5
    items = await s.get_items()
    assert len(items) == 3  # 1 摘要 + keep_last(2)
    assert items[0]["role"] == "system" and "先前對話摘要" in items[0]["content"]
    assert items[1]["content"] == "4" and items[2]["content"] == "5"  # 保留最後兩筆


async def test_summarizer_failure_preserves_history():
    inner = _FakeSession()

    async def boom(head):
        raise RuntimeError("summarizer down")

    s = SummarizingSession(inner, boom, max_items=5, keep_last=2)
    await s.add_items([{"role": "user", "content": str(i)} for i in range(6)])
    items = await s.get_items()
    assert len(items) == 6  # 壓縮失敗 → 不弄丟歷史


def test_keep_last_must_be_less_than_max():
    with pytest.raises(ValueError):
        SummarizingSession(_FakeSession(), _summ, max_items=5, keep_last=5)


def test_keep_last_zero_rejected():
    # keep_last=0 會踩 -0 切片陷阱 → 建構期擋掉。
    with pytest.raises(ValueError):
        SummarizingSession(_FakeSession(), _summ, max_items=5, keep_last=0)
