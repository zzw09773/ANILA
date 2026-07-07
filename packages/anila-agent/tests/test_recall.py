"""memdir hybrid recall：注入式引擎 + fail-closed 退場鏈。"""

from __future__ import annotations

import httpx
import pytest

from anila_agent.memory.recall import keyword_shortlist, make_embed_fn, recall

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


# ---- make_embed_fn：/embeddings 回傳 data[].index 對齊 ----------------------


class _FakeEmbedResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeEmbedClient:
    """假 httpx.AsyncClient：只認 POST，回固定 payload（不連網）。"""

    def __init__(self, payload: dict, **_kw) -> None:
        self._payload = payload

    async def __aenter__(self) -> _FakeEmbedClient:
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def post(self, *_a, **_kw) -> _FakeEmbedResponse:
        return _FakeEmbedResponse(self._payload)


async def test_make_embed_fn_realigns_out_of_order_index(monkeypatch):
    """``/embeddings`` 的 ``data[]`` 不保證與 input 同序 —— 只保證 index 對得回
    原始位置（query 在 input[0]，候選依 manifest 順序在 input[1:]）。這裡故意把
    data[] 倒序回傳，驗證排序後仍依 index 對齊，而非依陣列順序直接 zip。
    """
    manifest = {"m1": "desc1", "m2": "desc2"}
    # inputs = [query, desc(m1), desc(m2)] -> index 0, 1, 2。
    # data[] 陣列刻意打亂，但 index 標明真正位置。
    payload = {
        "data": [
            {"embedding": [1.0, 0.0], "index": 0},  # query
            {"embedding": [0.0, 1.0], "index": 2},  # m2（陣列位置在前，但 index=2）
            {"embedding": [1.0, 0.0], "index": 1},  # m1
        ]
    }

    def _client_factory(**kw):
        return _FakeEmbedClient(payload, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)
    embed_fn = make_embed_fn(base_url="http://embed.test", model="m")
    ranked = await embed_fn("q", manifest, 2)
    # 依 index 對齊後：query=[1,0]、m1=[1,0]（餘弦=1）、m2=[0,1]（餘弦=0）→ m1 最相關。
    # 若誤依 data[] 陣列順序 zip，m1/m2 的向量會互換，排名會反過來。
    assert ranked[0] == "m1"
