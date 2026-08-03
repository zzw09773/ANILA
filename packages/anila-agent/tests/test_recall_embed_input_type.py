"""記憶粗篩:查詢與候選描述必須分兩次呼叫,各自標明 input_type。

Triton 類 embedder 把查詢與文件放在**不同輸入張量**,一個請求只能是其中一側。
原本 ``make_embed_fn`` 把 ``[query, *descriptions]`` 併成一批送出,結果是查詢也
被當文件編碼 —— 不會報錯,只是相似度排序悄悄變差。CSP 的 query 側對批次會直接
結構性拒收(每次只收一個字串),所以合批的寫法在 Triton 上不是「品質差一點」,
是**整條 recall 400**。
"""
from __future__ import annotations

import json

import pytest

from anila_agent.memory.recall import make_embed_fn


class _Resp:
    def __init__(self, vectors):
        self._vectors = vectors

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": v} for v in self._vectors]}


class _Client:
    """記錄每一次 POST 的 body;依 input_type 回不同向量。"""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        body = json or {}
        _Client.calls.append(body)
        n = len(body["input"])
        if body.get("input_type") == "query":
            return _Resp([[1.0, 0.0]] * n)
        # 文件側:第一筆與查詢正交,第二筆與查詢同向 → 排序可預期
        return _Resp([[0.0, 1.0], [1.0, 0.0]][:n])


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _Client.calls = []
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    yield


async def test_query_and_documents_go_in_separate_requests():
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")
    manifest = {"甲": "甲的描述", "乙": "乙的描述"}

    await embed("查詢字串", manifest, 2)

    assert len(_Client.calls) == 2, "查詢與文件必須分兩次呼叫,不可合批"

    q_call, d_call = _Client.calls
    assert q_call["input_type"] == "query"
    assert q_call["input"] == ["查詢字串"], "query 側每次只能送一個字串"
    assert d_call["input_type"] == "document"
    assert d_call["input"] == ["甲的描述", "乙的描述"]
    # 查詢字串絕不可出現在文件側那一批裡。
    assert "查詢字串" not in d_call["input"]


async def test_ranking_still_uses_the_query_vector():
    """拆成兩次呼叫之後,排序結果仍然正確(不是為了通過測試而空轉)。"""
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")
    manifest = {"不相關": "甲的描述", "相關": "乙的描述"}

    picked = await embed("查詢字串", manifest, 1)

    # 文件側第二筆 [1,0] 與查詢 [1,0] 同向 → 應排第一。
    assert picked == ["相關"]


async def test_empty_manifest_makes_no_request():
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")
    assert await embed("查詢字串", {}, 3) == []
    assert _Client.calls == []
