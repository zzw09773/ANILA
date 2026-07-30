"""Tests for the 1-hop relation expansion in ``app.api.ingestion.search``
(design v2 §7, test plan §12: out-going / in-going carried out,
``max_related`` cap, ``relation_types`` filter, confidence threshold, related
never replaces the main top-k, empty when no relations).

The pgvector ``similarity_search_per_document`` needs Postgres, so we inject a
fake store returning canned per-document chunks; the edge-selection +
direction + cap + filter logic (the part that lives in the API layer) runs
against the SQLite ``db`` fixture. Neutral regulation names throughout.
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from anila_core.ingestion.citation_extractor import normalize_title

from app.api.ingestion.search import RelatedHit, SearchRequest, _expand_relations
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.user import User
from app.services import relation_resolver as rr


# ── helpers ──────────────────────────────────────────────────────────────────
def _collection(db) -> IngestionCollection:
    from tests.conftest import make_user

    owner: User = make_user(db)
    c = IngestionCollection(
        name="regs", chunking_config={"strategy": "fixed"},
        embedding_model="nvidia/NV-embed-V2", embedding_dim=4000, created_by=owner.id,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _doc(db, collection_id: int, title: str) -> IngestionDocument:
    d = IngestionDocument(
        collection_id=collection_id, filename=f"{title}.pdf", title=title,
        normalized_title=normalize_title(title),
        sha256=hashlib.sha256(f"{collection_id}:{title}".encode()).hexdigest(),
        status="indexed",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _hit(doc_id: int, chunk_id: int, content: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(
        chunk=SimpleNamespace(
            id=chunk_id, document_id=doc_id, chunk_key=f"c{chunk_id}", content=content
        ),
        score=score,
    )


class _FakeStore:
    """Stand-in for CollectionScopedPgVectorStore — returns canned per-doc hits."""

    def __init__(self, hits_by_doc: dict[int, SimpleNamespace]) -> None:
        self._by_doc = hits_by_doc
        self.last_doc_ids: list[int] | None = None

    async def similarity_search_per_document(
        self, *, query_embedding, document_ids, k=1, min_score=0.0
    ):
        self.last_doc_ids = list(document_ids)
        return [self._by_doc[d] for d in document_ids if d in self._by_doc]


def _expand(db, store, collection_id, main_ids, **req):
    payload = SearchRequest(query="q", expand_relations=True, **req)
    return asyncio.run(
        _expand_relations(
            db, store, collection_id=collection_id,
            main_doc_ids=set(main_ids), query_vec=[0.1, 0.2], payload=payload,
        )
    )


@pytest.fixture()
def coll(db):
    return _collection(db)


# ── out-going: main hit cites a related doc ──────────────────────────────────
def test_outgoing_expansion(db, coll):
    from anila_core.ingestion.citation_extractor import Citation

    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充規定")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[Citation("supplements", "母法", None, "母法", "補充母法")],
        run_id="r1",
    )
    db.commit()

    store = _FakeStore({parent.id: _hit(parent.id, 11, "母法第一條…", 0.8)})
    related = _expand(db, store, coll.id, [child.id], max_related=5)

    assert len(related) == 1
    r = related[0]
    assert isinstance(r, RelatedHit)
    assert r.document_id == parent.id
    assert r.direction == "outgoing"  # child → parent
    assert r.via_document_id == child.id
    assert r.relation_type == "supplements"
    assert r.content == "母法第一條…" and r.score == 0.8
    assert store.last_doc_ids == [parent.id]


# ── in-going: a related doc cites the main hit ───────────────────────────────
def test_incoming_expansion(db, coll):
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充規定")
    # child → parent edge; main hit is the PARENT, so child is incoming.
    from anila_core.ingestion.citation_extractor import Citation

    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[Citation("based_on", "母法", None, "母法", "依母法")],
        run_id="r1",
    )
    db.commit()

    store = _FakeStore({child.id: _hit(child.id, 22, "補充內容…", 0.6)})
    related = _expand(db, store, coll.id, [parent.id], max_related=5)

    assert len(related) == 1
    assert related[0].document_id == child.id
    assert related[0].direction == "incoming"  # child points at the main hit
    assert related[0].via_document_id == parent.id


# ── filters + cap ────────────────────────────────────────────────────────────
def _link(db, coll, src, title, rtype):
    from anila_core.ingestion.citation_extractor import Citation

    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=src,
        citations=[Citation(rtype, normalize_title(title), None, normalize_title(title), "x")],
        run_id="r1",
    )


def test_relation_type_filter(db, coll):
    a = _doc(db, coll.id, "甲辦法")
    b = _doc(db, coll.id, "乙辦法")
    child = _doc(db, coll.id, "子規定")
    # child has two outgoing edges of different types
    from anila_core.ingestion.citation_extractor import Citation

    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[
            Citation("based_on", "甲辦法", None, "甲辦法", "依甲"),
            Citation("supersedes", "乙辦法", None, "乙辦法", "廢止乙"),
        ],
        run_id="r1",
    )
    db.commit()

    store = _FakeStore({a.id: _hit(a.id, 1, "甲", 0.5), b.id: _hit(b.id, 2, "乙", 0.5)})
    related = _expand(db, store, coll.id, [child.id], relation_types=["based_on"])
    assert {r.document_id for r in related} == {a.id}
    assert related[0].relation_type == "based_on"


def test_max_related_cap(db, coll):
    child = _doc(db, coll.id, "子規定")
    targets = []
    from anila_core.ingestion.citation_extractor import Citation

    cites = []
    for i in range(4):
        t = _doc(db, coll.id, f"辦法{i}")
        targets.append(t)
        cites.append(Citation("cites", f"辦法{i}", None, normalize_title(f"辦法{i}"), "x"))
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id, citations=cites, run_id="r1"
    )
    db.commit()

    store = _FakeStore({t.id: _hit(t.id, t.id, "x", 0.5) for t in targets})
    related = _expand(db, store, coll.id, [child.id], max_related=2)
    assert len(related) == 2


def test_confidence_threshold(db, coll):
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充")
    from anila_core.ingestion.citation_extractor import Citation

    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[Citation("based_on", "母法", None, "母法", "依母法")], run_id="r1",
    )
    db.commit()
    # all rule edges are confidence 1.0 → a 0.5 floor keeps them, a >1 floor drops
    store = _FakeStore({parent.id: _hit(parent.id, 1, "x", 0.5)})
    assert len(_expand(db, store, coll.id, [child.id], min_relation_confidence=0.5)) == 1


def test_unresolved_edge_not_expanded(db, coll):
    child = _doc(db, coll.id, "補充")  # cites a doc that doesn't exist → dst NULL
    from anila_core.ingestion.citation_extractor import Citation

    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[Citation("based_on", "缺席母法", None, "缺席母法", "依")], run_id="r1",
    )
    db.commit()
    store = _FakeStore({})
    assert _expand(db, store, coll.id, [child.id], max_related=5) == []


def test_no_relations_returns_empty(db, coll):
    lone = _doc(db, coll.id, "孤兒文件")
    store = _FakeStore({})
    assert _expand(db, store, coll.id, [lone.id], max_related=5) == []


def test_expand_relations_snapshot_survives_orm_expire(db, coll, monkeypatch):
    """Edge attrs used after commit must come from a plain snapshot.

    Revert to bare attribute-touch + reading ORM ``picked[rel_doc]`` after
    commit, and expunging those instances makes RelatedHit construction raise
    DetachedInstanceError. Matches ``_embed_query``'s SimpleNamespace conclusion.
    """
    from anila_core.ingestion.citation_extractor import Citation
    from app.models.ingestion import DocumentRelation

    parent = _doc(db, coll.id, "母法-snap")
    child = _doc(db, coll.id, "補充-snap")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[Citation("supplements", "母法-snap", None, "母法-snap", "補")],
        run_id="r-snap",
    )
    db.commit()

    real_commit = db.commit

    def commit_and_expunge_relations():
        edges = [
            obj
            for obj in db.identity_map.values()
            if isinstance(obj, DocumentRelation)
        ]
        real_commit()
        for e in edges:
            # Expire first: loaded attrs on a merely-expunged instance remain
            # readable; expire+expunge makes post-commit ORM reads fail.
            db.expire(e)
            db.expunge(e)

    monkeypatch.setattr(db, "commit", commit_and_expunge_relations)
    store = _FakeStore({parent.id: _hit(parent.id, 99, "snap…", 0.7)})
    related = _expand(db, store, coll.id, [child.id], max_related=5)
    assert len(related) == 1
    assert related[0].relation_type == "supplements"
    assert related[0].confidence is not None
