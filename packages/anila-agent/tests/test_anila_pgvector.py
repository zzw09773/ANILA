"""ANILA 原生 pgvector retriever 的 SQL 形狀契約（平台契約，逐字）。"""

from __future__ import annotations

import pytest

from anila_agent.retrieval.anila_pgvector import (
    AnilaPgVectorRetriever,
    _format_halfvec,
    _normalize_dsn,
    _parse_metadata,
    from_env,
)

pytestmark = pytest.mark.unit


# ---- 純 helper ----

def test_format_halfvec():
    assert _format_halfvec([1.0, 2.5, 3.0]) == "[1,2.5,3]"


def test_normalize_dsn_strips_sqlalchemy_flavour():
    assert _normalize_dsn("postgresql+psycopg2://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert _normalize_dsn("postgresql://u@h/db") == "postgresql://u@h/db"


def test_parse_metadata_variants():
    assert _parse_metadata('{"a": 1}') == {"a": 1}
    assert _parse_metadata({"x": 1}) == {"x": 1}
    assert _parse_metadata(None) == {}
    assert _parse_metadata("not json") == {}


# ---- __init__ 驗證 ----

def test_rejects_non_positive_collection_id():
    kw = dict(url="postgresql://u@h/db", embed_base_url="http://e/v1", embed_api_key="k", embed_model="m")
    with pytest.raises(ValueError):
        AnilaPgVectorRetriever(collection_id=0, **kw)
    with pytest.raises(ValueError):
        AnilaPgVectorRetriever(collection_id=True, **kw)  # bool 不算 int


# ---- search SQL 契約 ----

class _Acquire:
    def __init__(self, conn): self._conn = conn
    async def __aenter__(self): return self._conn
    async def __aexit__(self, *a): return False


class _Txn:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Conn:
    def __init__(self, rows, cap):
        self._rows = rows
        self.cap = cap

    def transaction(self): return _Txn()
    async def execute(self, sql, *a): self.cap.setdefault("execute", []).append(sql)
    async def fetch(self, sql, *a):
        self.cap["fetch_sql"] = sql
        self.cap["fetch_args"] = a
        return self._rows


class _Pool:
    def __init__(self, conn): self._conn = conn
    def acquire(self): return _Acquire(self._conn)


async def test_search_sql_contract(monkeypatch):
    rows = [
        {"id": 11, "document_id": 3, "chunk_key": "k", "content": "hi", "metadata": '{"a": 1}', "score": 0.8}
    ]
    cap: dict = {}
    r = AnilaPgVectorRetriever(
        url="postgresql://u@h/db", collection_id=5,
        embed_base_url="http://e/v1", embed_api_key="k", embed_model="m",
    )
    r._pool = _Pool(_Conn(rows, cap))
    r._embed_dim = 4

    async def fake_embed(text):
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(r, "_embed", fake_embed)

    docs = await r.search("q", k=3)

    # RLS：SET LOCAL anila.collection_id = <id>
    assert any("SET LOCAL anila.collection_id = 5" in s for s in cap["execute"])
    sql = cap["fetch_sql"]
    assert "::halfvec" in sql  # halfvec 明確轉型
    assert "chunk_type = 'leaf'" in sql  # leaf 過濾
    assert "embedding <=> $1::halfvec" in sql  # cosine 距離
    emb_text, k = cap["fetch_args"]
    assert emb_text.startswith("[") and emb_text.endswith("]") and k == 3
    d = docs[0]
    assert d.id == "11" and d.score == 0.8
    assert d.metadata["a"] == 1  # JSONB 解析
    assert d.metadata["chunk_key"] == "k" and d.metadata["document_id"] == 3


async def test_search_pads_short_embedding(monkeypatch):
    cap: dict = {}
    r = AnilaPgVectorRetriever(
        url="postgresql://u@h/db", collection_id=5,
        embed_base_url="http://e/v1", embed_api_key="k", embed_model="m",
    )
    r._pool = _Pool(_Conn([], cap))
    r._embed_dim = 6  # 比 embed 回的 4 維大 → 補零到 6

    async def fake_embed(text):
        return [1.0, 2.0, 3.0, 4.0]

    monkeypatch.setattr(r, "_embed", fake_embed)
    await r.search("q")
    emb_text = cap["fetch_args"][0]
    assert emb_text.count(",") == 5  # 6 個值 → 5 個逗號


# ---- from_env ----

def test_from_env_none_without_collection_id(monkeypatch):
    monkeypatch.delenv("ANILA_COLLECTION_ID", raising=False)
    assert from_env() is None


def test_from_env_raises_on_partial_config(monkeypatch):
    monkeypatch.setenv("ANILA_COLLECTION_ID", "5")
    monkeypatch.delenv("PGVECTOR_URL", raising=False)
    with pytest.raises(ValueError, match="PGVECTOR_URL"):
        from_env()


def test_from_env_rejects_non_int_collection_id(monkeypatch):
    monkeypatch.setenv("ANILA_COLLECTION_ID", "abc")
    with pytest.raises(ValueError, match="must be an int"):
        from_env()
