"""Tests for ``app.services.relation_resolver`` — resolution + reconciliation
of cross-document relations (design v2 §6, test plan §12).

Runs against the SQLite ``db`` fixture (create_all). Covers: out-going
hit/miss, in-going back-fill (order-independent — supplement-first AND
parent-first), re-extract delete-old-keep-manual, ambiguous (>1 match),
idempotent re-run, self-citation exclusion, manual edge create/delete.

Fixtures use NEUTRAL regulation names (generic, no real-world labels).
"""
from __future__ import annotations

import hashlib

import pytest

from anila_core.ingestion.citation_extractor import Citation, normalize_title

from app.models.ingestion import DocumentRelation, IngestionCollection, IngestionDocument
from app.models.user import User
from app.services import relation_resolver as rr


# ── helpers ──────────────────────────────────────────────────────────────────
def _owner(db) -> User:
    from tests.conftest import make_user

    return make_user(db)


def _collection(db, owner: User) -> IngestionCollection:
    c = IngestionCollection(
        name="regs",
        chunking_config={"strategy": "fixed"},
        embedding_model="nvidia/NV-embed-V2",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4000,
        created_by=owner.id,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _doc(db, collection_id: int, title: str) -> IngestionDocument:
    sha = hashlib.sha256(f"{collection_id}:{title}".encode()).hexdigest()
    d = IngestionDocument(
        collection_id=collection_id,
        filename=f"{title}.pdf",
        title=title,
        normalized_title=normalize_title(title),
        sha256=sha,
        status="indexed",
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _cite(target_title: str, relation_type: str = "based_on", article: str | None = None) -> Citation:
    name = normalize_title(target_title)
    ref = f"{name} {article}" if article else name
    return Citation(
        relation_type=relation_type,
        target_title=name,
        article=article,
        target_ref=ref,
        evidence=f"依{target_title}{article or ''}訂定",
    )


def _edges(db, collection_id: int, src: int) -> list[DocumentRelation]:
    return (
        db.query(DocumentRelation)
        .filter(
            DocumentRelation.collection_id == collection_id,
            DocumentRelation.src_document_id == src,
        )
        .order_by(DocumentRelation.id)
        .all()
    )


@pytest.fixture()
def coll(db):
    owner = _owner(db)
    return _collection(db, owner)


# ── out-going ────────────────────────────────────────────────────────────────
def test_outgoing_hit(db, coll):
    parent = _doc(db, coll.id, "員工差勤管理辦法")
    child = _doc(db, coll.id, "員工差勤管理補充規定")

    counts = rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("員工差勤管理辦法", "based_on", "第3條")], run_id="run-1",
    )
    db.commit()

    assert counts.resolved == 1
    edges = _edges(db, coll.id, child.id)
    assert len(edges) == 1
    assert edges[0].source == "rule"
    assert edges[0].relation_type == "based_on"
    assert edges[0].dst_document_id == parent.id
    assert edges[0].target_ref == "員工差勤管理辦法 第3條"


def test_outgoing_miss_stays_unresolved(db, coll):
    child = _doc(db, coll.id, "補充規定")
    counts = rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("尚未上傳的母法")], run_id="run-1",
    )
    db.commit()
    assert counts.resolved == 0 and counts.still_unresolved == 1
    assert _edges(db, coll.id, child.id)[0].dst_document_id is None


# ── in-going back-fill (order independence) ──────────────────────────────────
def test_backfill_supplement_first_then_parent(db, coll):
    # 補充先到：cites the parent before the parent exists → unresolved …
    child = _doc(db, coll.id, "資安管理補充規定")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("公司資安管理辦法", "supplements")], run_id="r1",
    )
    db.commit()
    assert _edges(db, coll.id, child.id)[0].dst_document_id is None

    # … 母法後到：uploading the parent + reconciling back-fills the edge.
    parent = _doc(db, coll.id, "公司資安管理辦法")
    rr.resolve_pending(db, collection_id=coll.id)
    db.commit()
    assert _edges(db, coll.id, child.id)[0].dst_document_id == parent.id


def test_parent_first_then_supplement(db, coll):
    # 母法先到 then 補充 — child resolves on its own ingest, no extra pass.
    parent = _doc(db, coll.id, "公司資安管理辦法")
    child = _doc(db, coll.id, "資安管理補充規定")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("公司資安管理辦法", "supplements")], run_id="r1",
    )
    db.commit()
    assert _edges(db, coll.id, child.id)[0].dst_document_id == parent.id


# ── re-extract: delete old rule, keep manual ─────────────────────────────────
def test_reextract_replaces_rule_keeps_manual(db, coll):
    _doc(db, coll.id, "甲辦法")
    parent_b = _doc(db, coll.id, "乙辦法")
    child = _doc(db, coll.id, "子規定")

    # first extraction → one rule edge to 甲辦法
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("甲辦法", "based_on")], run_id="r1",
    )
    # a human adds a manual edge
    rr.create_manual_edge(
        db, collection_id=coll.id, src_document_id=child.id,
        relation_type="cites", dst_document_id=parent_b.id, created_by_user_id=None,
    )
    db.commit()
    assert {e.source for e in _edges(db, coll.id, child.id)} == {"rule", "manual"}

    # re-extract with a DIFFERENT citation → old rule replaced, manual survives
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("乙辦法", "amends")], run_id="r2",
    )
    db.commit()

    edges = _edges(db, coll.id, child.id)
    rule = [e for e in edges if e.source == "rule"]
    manual = [e for e in edges if e.source == "manual"]
    assert len(rule) == 1 and rule[0].relation_type == "amends"
    assert rule[0].dst_document_id == parent_b.id  # 乙辦法
    assert rule[0].extractor_run_id == "r2"
    assert len(manual) == 1 and manual[0].dst_document_id == parent_b.id


# ── ambiguous ────────────────────────────────────────────────────────────────
def test_ambiguous_multiple_matches_left_null(db, coll):
    # two docs share the same normalized title
    _doc(db, coll.id, "作業規定")
    db.add(
        IngestionDocument(
            collection_id=coll.id, filename="dup.pdf", title="作業規定",
            normalized_title=normalize_title("作業規定"),
            sha256="f" * 64, status="indexed",
        )
    )
    db.commit()
    child = _doc(db, coll.id, "引用方")

    counts = rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("作業規定", "cites")], run_id="r1",
    )
    db.commit()
    assert counts.ambiguous == 1 and counts.resolved == 0
    edge = _edges(db, coll.id, child.id)[0]
    assert edge.dst_document_id is None
    flags = rr.annotate_ambiguity(db, collection_id=coll.id, edges=[edge])
    assert flags[edge.id] is True


# ── idempotency + self-citation ──────────────────────────────────────────────
def test_resolve_pending_idempotent(db, coll):
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("母法", "based_on")], run_id="r1",
    )
    db.commit()
    first = rr.resolve_pending(db, collection_id=coll.id)
    second = rr.resolve_pending(db, collection_id=coll.id)
    db.commit()
    # nothing left to resolve on the repeat run
    assert first.resolved == 0 and second.resolved == 0
    assert _edges(db, coll.id, child.id)[0].dst_document_id == parent.id
    # exactly one rule edge (no duplication across re-runs)
    assert len(_edges(db, coll.id, child.id)) == 1


def test_self_citation_not_resolved_to_self(db, coll):
    # a doc whose title equals the cited name must not point at itself
    doc = _doc(db, coll.id, "自我參照辦法")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=doc.id,
        citations=[_cite("自我參照辦法", "cites")], run_id="r1",
    )
    db.commit()
    assert _edges(db, coll.id, doc.id)[0].dst_document_id is None


# ── manual edge CRUD ─────────────────────────────────────────────────────────
def test_create_manual_edge_resolved(db, coll):
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充")
    edge = rr.create_manual_edge(
        db, collection_id=coll.id, src_document_id=child.id,
        relation_type="supplements", dst_document_id=parent.id, created_by_user_id=None,
    )
    db.commit()
    assert edge.source == "manual" and edge.dst_document_id == parent.id


def test_create_manual_edge_unresolved_then_backfilled(db, coll):
    child = _doc(db, coll.id, "補充")
    edge = rr.create_manual_edge(
        db, collection_id=coll.id, src_document_id=child.id,
        relation_type="supplements", target_ref="《尚未上傳母法》", created_by_user_id=None,
    )
    db.commit()
    assert edge.dst_document_id is None
    assert edge.target_ref == "尚未上傳母法"  # normalized: brackets stripped

    parent = _doc(db, coll.id, "尚未上傳母法")
    rr.resolve_pending(db, collection_id=coll.id)
    db.commit()
    db.refresh(edge)
    assert edge.dst_document_id == parent.id


def test_create_manual_edge_bad_dst_rejected(db, coll):
    child = _doc(db, coll.id, "補充")
    with pytest.raises(ValueError):
        rr.create_manual_edge(
            db, collection_id=coll.id, src_document_id=child.id,
            relation_type="cites", dst_document_id=999999,
        )


def test_delete_only_manual(db, coll):
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充")
    rr.apply_document_extraction(
        db, collection_id=coll.id, src_document_id=child.id,
        citations=[_cite("母法", "based_on")], run_id="r1",
    )
    manual = rr.create_manual_edge(
        db, collection_id=coll.id, src_document_id=child.id,
        relation_type="cites", dst_document_id=parent.id,
    )
    db.commit()
    rule_id = [e for e in _edges(db, coll.id, child.id) if e.source == "rule"][0].id

    assert rr.delete_manual_edge(db, rel_id=manual.id) is True
    assert rr.delete_manual_edge(db, rel_id=rule_id) is False  # rule edges protected
    db.commit()
    remaining = _edges(db, coll.id, child.id)
    assert len(remaining) == 1 and remaining[0].source == "rule"


# ── reresolve whole collection ───────────────────────────────────────────────
def test_reresolve_collection(db, coll):
    a = _doc(db, coll.id, "母法")
    b = _doc(db, coll.id, "補充")
    result = rr.reresolve_collection(
        db, collection_id=coll.id,
        citations_by_doc={
            b.id: [_cite("母法", "supplements")],
            a.id: [],
        },
        run_id="rr1",
    )
    db.commit()
    assert result.rule_edges_extracted == 1
    assert result.resolved == 1
    assert _edges(db, coll.id, b.id)[0].dst_document_id == a.id
