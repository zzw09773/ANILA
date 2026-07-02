"""API tests for the cross-document relations endpoints (design v2 §8).

TestClient over the SQLite fixture. Covers: manual create (resolved +
unresolved), bad src 404, list with flags, delete (manual ok / rule protected /
idempotent 404), reresolve (synchronous reconcile; the async worker enqueue is
best-effort and absent in tests), and ACL (non-owner 403/404).

Bearer auth is used so requests bypass CSRF (the cookie path is what
CsrfMiddleware guards). Neutral regulation names throughout.
"""
from __future__ import annotations

import hashlib
import os

# The app's startup security guard refuses to boot on dev-default secrets unless
# this dev opt-in is set; the TestClient triggers that lifespan. Set before any
# app import / client fixture runs (mirrors the other client-based test files).
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from anila_core.ingestion.citation_extractor import normalize_title

from app.models.ingestion import DocumentRelation, IngestionCollection, IngestionDocument
from tests.conftest import make_user


def _coll(db, owner_id: int) -> IngestionCollection:
    c = IngestionCollection(
        name="regs", chunking_config={"strategy": "fixed"},
        embedding_model="nvidia/NV-embed-V2", embedding_dim=4000, created_by=owner_id,
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


def _auth(client, username="alice"):
    return {"Authorization": f"Bearer {_login(client, username)}"}


def _login(client, username):
    resp = client.post("/api/auth/login", json={"username": username, "password": "password"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ── create ───────────────────────────────────────────────────────────────────
def test_create_manual_edge_resolved(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充規定")

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/relations",
        json={
            "src_document_id": child.id,
            "relation_type": "supplements",
            "dst_document_id": parent.id,
        },
        headers=_auth(client),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "manual"
    assert body["resolved"] is True
    assert body["dst_document_id"] == parent.id
    assert body["dst_title"] == "母法"
    assert body["src_title"] == "補充規定"


def test_create_unresolved_then_listed_flagged(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    child = _doc(db, coll.id, "補充規定")

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/relations",
        json={
            "src_document_id": child.id,
            "relation_type": "based_on",
            "target_ref": "尚未上傳母法",
        },
        headers=_auth(client),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["resolved"] is False

    listing = client.get(
        f"/api/ingestion/collections/{coll.id}/relations", headers=_auth(client)
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["resolved"] is False
    assert rows[0]["target_ref"] == "尚未上傳母法"


def test_create_bad_src_404(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/relations",
        json={"src_document_id": 999999, "relation_type": "cites", "target_ref": "X辦法"},
        headers=_auth(client),
    )
    assert resp.status_code == 404


def test_create_requires_a_target_422(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    child = _doc(db, coll.id, "補充")
    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/relations",
        json={"src_document_id": child.id, "relation_type": "cites"},
        headers=_auth(client),
    )
    assert resp.status_code == 422  # schema validator: one of dst/target_ref


# ── delete ───────────────────────────────────────────────────────────────────
def test_delete_manual_then_404(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充")
    created = client.post(
        f"/api/ingestion/collections/{coll.id}/relations",
        json={"src_document_id": child.id, "relation_type": "cites", "dst_document_id": parent.id},
        headers=_auth(client),
    ).json()
    rid = created["id"]

    d1 = client.delete(
        f"/api/ingestion/relations/{rid}?collection_id={coll.id}", headers=_auth(client)
    )
    assert d1.status_code == 204
    d2 = client.delete(
        f"/api/ingestion/relations/{rid}?collection_id={coll.id}", headers=_auth(client)
    )
    assert d2.status_code == 404


def test_delete_rule_edge_protected(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    child = _doc(db, coll.id, "補充")
    rule = DocumentRelation(
        collection_id=coll.id, src_document_id=child.id, dst_document_id=None,
        target_ref="某辦法", relation_type="based_on", confidence=1.0, source="rule",
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    resp = client.delete(
        f"/api/ingestion/relations/{rule.id}?collection_id={coll.id}", headers=_auth(client)
    )
    assert resp.status_code == 404  # rule edges are not deletable here


# ── reresolve ────────────────────────────────────────────────────────────────
def test_reresolve_reconciles(client, db):
    alice = make_user(db, "alice")
    coll = _coll(db, alice.id)
    parent = _doc(db, coll.id, "母法")
    child = _doc(db, coll.id, "補充")
    # an unresolved rule edge to 母法 (e.g. parent uploaded after the rule edge)
    db.add(
        DocumentRelation(
            collection_id=coll.id, src_document_id=child.id, dst_document_id=None,
            target_ref="母法", relation_type="based_on", confidence=1.0, source="rule",
        )
    )
    db.commit()

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/relations:reresolve", headers=_auth(client)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"] == 1

    rows = client.get(
        f"/api/ingestion/collections/{coll.id}/relations", headers=_auth(client)
    ).json()
    assert rows[0]["dst_document_id"] == parent.id


# ── ACL ──────────────────────────────────────────────────────────────────────
def test_non_owner_cannot_list(client, db):
    alice = make_user(db, "alice")
    make_user(db, "mallory")
    coll = _coll(db, alice.id)
    resp = client.get(
        f"/api/ingestion/collections/{coll.id}/relations", headers=_auth(client, "mallory")
    )
    assert resp.status_code in (403, 404)


def test_non_owner_cannot_create(client, db):
    alice = make_user(db, "alice")
    make_user(db, "mallory")
    coll = _coll(db, alice.id)
    child = _doc(db, coll.id, "補充")
    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/relations",
        json={"src_document_id": child.id, "relation_type": "cites", "target_ref": "X辦法"},
        headers=_auth(client, "mallory"),
    )
    assert resp.status_code in (403, 404)
