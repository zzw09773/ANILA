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


class _ShortRespClient(_Client):
    """文件側少回一個向量 —— 端點壞掉、批次被截斷時真的會發生。"""

    async def post(self, url, headers=None, json=None):
        body = json or {}
        _Client.calls.append(body)
        if body.get("input_type") == "query":
            return _Resp([[1.0, 0.0]])
        return _Resp([[0.0, 1.0]])  # 送 2 段,只回 1 個


class _NoQueryVectorClient(_Client):
    """查詢側一個向量都沒回 —— 端點回了 ``data: []``。"""

    async def post(self, url, headers=None, json=None):
        body = json or {}
        _Client.calls.append(body)
        if body.get("input_type") == "query":
            return _Resp([])
        return _Resp([[0.0, 1.0], [1.0, 0.0]])


# ── 兩側各一道防線,而且要各自可被殺死 ──────────────────────────────────────
#
# 這兩支的重點不只是「有擋到」,是**互不遮蔽**。前一版把兩道檢查疊在同一個
# 地方(``_post`` 的長度檢查 + ``zip(strict=True)``),任拿掉一道,另一道都會
# 替它把測試撐綠 —— 兩道都在,卻等於一道都沒釘住。所以底下刻意一支對一道:
#   拿掉 ``strict=True``            → 只有 test_a_short_vector_batch... 變紅
#   拿掉查詢側的 ``len(q_vecs)!=1`` → 只有 test_a_missing_query_vector... 變紅


async def test_a_short_vector_batch_is_not_silently_truncated(monkeypatch):
    """少回一個向量 = 少一條候選記憶,而且沒有任何人會知道。

    ``zip(names, doc_vecs, strict=False)`` 時向量比文字少,zip 直接把尾巴吃掉,
    ``embed_fn`` 照樣回一份「看起來正常、只是短了」的排序。現在拋 ——
    ``recall()`` 會接住並退回 keyword 粗篩(候選一條不少),那比一份被無聲
    截短的排序誠實。

    刻意不對訊息內容斷言:這裡拋的是 ``zip`` 自己的 ValueError,把它的字面
    訊息寫進斷言只會綁死 CPython 的用字。要釘的是「有沒有拋」,而
    ``strict=True`` 一旦被拿掉就不會拋。
    """
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _ShortRespClient)
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")

    with pytest.raises(ValueError):
        await embed("查詢字串", {"甲": "甲的描述", "乙": "乙的描述"}, 2)


async def test_a_missing_query_vector_is_reported_not_indexed(monkeypatch):
    """查詢側回空清單:要說「端點回了 0 個向量」,不是 IndexError。

    兩者都會被 ``recall()`` 接住,所以行為上都退回 keyword —— 差別在現場看到
    的是哪一種 log。少了那道檢查,``q_vecs[0]`` 直接 IndexError,訊息裡不會有
    「端點」「查詢」「0 個向量」任何一個字,操作者會往程式的方向查而不是往
    端點的方向查。IndexError 不是 ValueError,所以這支會變紅。
    """
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _NoQueryVectorClient)
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")

    with pytest.raises(ValueError) as exc:
        await embed("查詢字串", {"甲": "甲的描述", "乙": "乙的描述"}, 2)

    message = str(exc.value)
    assert "查詢" in message and "0 個向量" in message


async def test_recall_falls_back_to_keywords_when_the_query_vector_is_missing(
    monkeypatch,
):
    """整條 recall 的行為:查詢側壞掉時候選數一樣不減。"""
    import httpx

    from anila_agent.memory.recall import recall

    monkeypatch.setattr(httpx, "AsyncClient", _NoQueryVectorClient)
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")
    manifest = {"甲": "甲的描述", "乙": "乙的描述"}

    names = await recall("查詢字串", manifest, embed_fn=embed, k=5)

    assert set(names) == set(manifest)


async def test_recall_falls_back_to_keywords_instead_of_a_short_list(monkeypatch):
    """整條 recall 的行為:候選數不減,而不是悄悄少一條。"""
    import httpx

    from anila_agent.memory.recall import recall

    monkeypatch.setattr(httpx, "AsyncClient", _ShortRespClient)
    embed = make_embed_fn(base_url="http://csp:8000/v1", model="m", api_key="k")
    manifest = {"甲": "甲的描述", "乙": "乙的描述"}

    names = await recall("查詢字串", manifest, embed_fn=embed, k=5)

    assert set(names) == set(manifest), "粗篩失敗時應退回 keyword,候選不該變少"
