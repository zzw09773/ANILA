"""查詢擴展（民國紀年＋域內同義）與 search_collection hook 接線測試。"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.auth_service import create_tokens
from app.services.search_expansion import expand_query

from tests.conftest import make_user


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANILA_QUERY_EXPANSION", raising=False)


# ── expand_query 單元（既有正向）────────────────────────────────────────────


def test_expand_roc_du_to_western(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    out = expand_query("113年度測評結果")
    assert out.startswith("113年度測評結果")
    assert "2024" in out


def test_expand_western_to_roc(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    out = expand_query("2024年測試")
    assert "民國113" in out


def test_expand_lidar_synonyms(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    out = expand_query("激光雷達的測距精度")
    assert "光達" in out
    assert "雷射" in out


def test_expand_ncsist_synonym(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    out = expand_query("中科院的無人機")
    assert "NCSIST" in out
    assert "國家中山科學研究院" in out


def test_pure_english_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    q = "radar ranging accuracy report"
    assert expand_query(q) == q


def test_flag_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANILA_QUERY_EXPANSION", "0")
    assert expand_query("113年度測評結果") == "113年度測評結果"
    assert expand_query("激光雷達") == "激光雷達"


# ── F2 / F3 / F6 負向：不得擴展 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "q",
    [
        "2113年度計畫",
        "編號113年度計畫",
        "ISO 2024",
        "型號2024",
        "113年式步槍",
        "F-16",
        "113",
        "79年",
        "131年",
    ],
)
def test_no_expansion_precision_negatives(
    monkeypatch: pytest.MonkeyPatch, q: str,
) -> None:
    _on(monkeypatch)
    out = expand_query(q)
    assert out == q


def test_cas_disambiguation_no_ncsist(monkeypatch: pytest.MonkeyPatch) -> None:
    """含『中國科學院』時不得把中科院擴成 NCSIST／國家中山科學研究院。"""
    _on(monkeypatch)
    q = "中國科學院（中科院）量子研究"
    out = expand_query(q)
    assert "NCSIST" not in out
    assert "國家中山科學研究院" not in out


# ── F6 / F4 正向 ────────────────────────────────────────────────────────────


def test_bare_roc_year_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    assert "2024" in expand_query("113年測評")
    assert "2024" in expand_query("113 年報告")


def test_western_range_symmetric_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    assert "民國130" in expand_query("2041年")
    assert "民國80" in expand_query("1991年")


def test_ncsist_still_expands_without_cas(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    out = expand_query("中科院的無人機")
    assert "國家中山科學研究院" in out
    assert "NCSIST" in out


def test_western_date_slash_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    assert "民國113" in expand_query("截止 2024/05")


# ── search_collection hook ──────────────────────────────────────────────────


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


class _StubStore:
    async def similarity_search(self, **_kwargs):
        return []


def test_search_collection_embeds_expanded_query(
    client: TestClient, db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook：expand_query 後的字串才進 _embed_query。"""
    _on(monkeypatch)
    alice = make_user(db, username="exp_alice", role="user")
    coll = IngestionCollection(
        name="exp-coll",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    db.add(
        IngestionDocument(
            collection_id=coll.id,
            filename="a.pdf",
            sha256="b" * 64,
            mime_type="application/pdf",
            status="indexed",
        )
    )
    db.commit()

    captured: dict[str, str] = {}

    async def fake_embed(db_, user, model_name, dim, query):
        captured["query"] = query
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed)
    monkeypatch.setattr(
        search_mod,
        "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _StubStore(),
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        json={"query": "113年度測評結果", "top_k": 3, "min_score": 0.0},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert "2024" in captured["query"]
    # 回應仍回傳使用者原始 query
    assert resp.json()["query"] == "113年度測評結果"


def test_search_collection_skips_expansion_when_flag_off(
    client: TestClient, db, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_QUERY_EXPANSION", "0")
    alice = make_user(db, username="exp_bob", role="user")
    coll = IngestionCollection(
        name="exp-coll-off",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)

    captured: dict[str, str] = {}

    async def fake_embed(db_, user, model_name, dim, query):
        captured["query"] = query
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed)
    monkeypatch.setattr(
        search_mod,
        "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _StubStore(),
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        json={"query": "113年度測評結果", "top_k": 3},
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    assert captured["query"] == "113年度測評結果"
