"""embedding-debug must not 500 on a heading chunk with a NULL vector.

Live: inspector 「載入向量維度＋範數」 on a heading row (doc 61: 15
headings all NULL) hit ``list(None)`` → TypeError → 500.

Invariant: no-vector chunks return 200-shaped payload with a note that
says the block was never embedded — not dim 0 pretending to be a
broken vector. Leaf rows stay dim/norm only.

The helper tests pin the mapping. The route test is what pins
「這條路由不再 500」— TestClient hits the real path. Mutant: drop the
None branch → helper TypeError *and* the route goes 500.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.ingestion.documents as documents_mod
from app.api.ingestion.documents import (
    HEADING_NO_EMBEDDING_NOTE,
    _embedding_debug_from_value,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services.auth_service import create_tokens

from tests.conftest import make_user


class _Half:
    def __init__(self, values):
        self._values = values

    def to_list(self):
        return list(self._values)


_CONTRACT = json.loads(
    Path(__file__).with_name("fixtures").joinpath(
        "chunk_embedding_debug_contract.json"
    ).read_text()
)


def test_producer_wire_matches_shared_contract():
    """Rename chunk_id/dim/norm/note here and the UI test must also go red."""
    heading = _embedding_debug_from_value(
        _CONTRACT["heading"]["chunk_id"], None,
    ).model_dump()
    assert heading == _CONTRACT["heading"]
    for key in _CONTRACT["required_keys"]:
        assert key in heading


def test_heading_null_embedding_is_not_a_typeerror():
    """Unfixed: list(None) raises. Fixed: note, dim 0, norm None."""
    out = _embedding_debug_from_value(61, None)
    assert out.chunk_id == 61
    assert out.dim == 0
    assert out.norm is None
    assert out.note == HEADING_NO_EMBEDDING_NOTE
    assert "heading" in out.note
    dumped = out.model_dump()
    assert dumped["note"] == HEADING_NO_EMBEDDING_NOTE
    assert dumped["norm"] is None


def test_leaf_halfvector_still_reports_dim_and_norm():
    out = _embedding_debug_from_value(14, _Half([3.0, 4.0]))
    assert out.chunk_id == 14
    assert out.dim == 2
    assert out.norm == pytest.approx(5.0)
    assert out.note is None


def test_leaf_plain_list_still_reports_dim_and_norm():
    out = _embedding_debug_from_value(7, [0.0, 1.0])
    assert out.dim == 2
    assert out.norm == pytest.approx(1.0)
    assert out.note is None


class _HeadingPool:
    """Pool whose fetchrow returns a heading row with embedding=None."""

    def __init__(self, chunk_id: int) -> None:
        self._chunk_id = chunk_id

    def acquire(self):
        chunk_id = self._chunk_id

        class _Txn:
            async def start(self):
                return None

            async def commit(self):
                return None

            async def rollback(self):
                return None

        class _Conn:
            def transaction(self):
                return _Txn()

            async def execute(self, _sql, *args):
                return None

            async def fetchrow(self, _sql, *args):
                return {"id": chunk_id, "embedding": None}

        @asynccontextmanager
        async def _acq():
            yield _Conn()

        return _acq()


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def test_heading_chunk_route_returns_200_with_note(
    client: TestClient, db, monkeypatch,
):
    """The 500 lived on the route. Hitting the helper is not enough."""
    alice = make_user(db, username="alice-vecdebug", role="user")
    coll = IngestionCollection(
        name="vecdebug-coll",
        chunking_config={"strategy": "hierarchical"},
        embedding_model="nv-embed",
        embedding_dim=4096,
        status="active",
        created_by=alice.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    doc = IngestionDocument(
        collection_id=coll.id,
        filename="guide.pdf",
        sha256="b" * 64,
        mime_type="application/pdf",
        status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    import app.services.ingestion_pool as ingestion_pool

    monkeypatch.setattr(ingestion_pool, "get_pool", lambda: _HeadingPool(61))

    resp = client.get(
        f"/api/ingestion/documents/{doc.id}/chunks/61/embedding-debug",
        headers=_bearer(alice),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "chunk_id": 61,
        "dim": 0,
        "norm": None,
        "note": HEADING_NO_EMBEDDING_NOTE,
    }
