"""memdir hybrid recall：注入式引擎 + fail-closed 退場鏈。"""

from __future__ import annotations

import pytest

from anila_agent.memory.recall import keyword_shortlist, recall

pytestmark = pytest.mark.unit

_MANIFEST = {
    "max-model-len": "不要縮 max model len 換記憶體",
    "no-bug": "不要腦補成 bug",
    "card-auth": "卡片登入安全稽核 CRITICAL",
    "branch": "main 與 prod 雙線並維",
}


def test_keyword_shortlist_ranks_overlap():
    out = keyword_shortlist("卡片登入安全", _MANIFEST, 2)
    assert out[0] == "card-auth"


async def test_recall_empty_manifest():
    assert await recall("q", {}) == []


async def test_recall_uses_embed_then_select():
    async def embed_fn(q, manifest, n):
        return ["card-auth", "branch", "no-bug"][:n]

    async def select_fn(q, sub, k):
        return ["card-auth"]

    out = await recall("卡片", _MANIFEST, embed_fn=embed_fn, select_fn=select_fn, k=5, shortlist=3)
    assert out == ["card-auth"]


async def test_recall_embed_failure_falls_back_to_keyword():
    async def embed_fn(q, manifest, n):
        raise RuntimeError("embed down")

    out = await recall("卡片登入安全", _MANIFEST, embed_fn=embed_fn, k=2, shortlist=2)
    assert "card-auth" in out  # 退回關鍵字粗篩


async def test_recall_select_failure_returns_shortlist():
    async def embed_fn(q, manifest, n):
        return ["branch", "no-bug"][:n]

    async def select_fn(q, sub, k):
        raise RuntimeError("select down")

    out = await recall("q", _MANIFEST, embed_fn=embed_fn, select_fn=select_fn, k=5, shortlist=2)
    assert out == ["branch", "no-bug"]  # 退回粗篩結果


async def test_recall_filters_hallucinated_names():
    async def embed_fn(q, manifest, n):
        return list(manifest)[:n]

    async def select_fn(q, sub, k):
        return ["card-auth", "not-a-real-memory"]  # 第二個是幻覺

    out = await recall("q", _MANIFEST, embed_fn=embed_fn, select_fn=select_fn)
    assert out == ["card-auth"]
