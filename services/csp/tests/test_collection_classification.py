# -*- coding: utf-8 -*-
"""Collection classification_level governance surface.

Covers:
- CollectionResponse exposes classification_level
- PATCH classification_level is admin/owner-only
- raise goes through apply_classification (ClassificationEvent + latch)
- lower is rejected with declassification-request flow hint
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.classification import ClassificationEvent
from app.models.ingestion import IngestionCollection
from tests.conftest import login, make_user


def _make_collection(db, owner, *, name="kb-1", level="無機密") -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        description=None,
        chunking_config={"strategy": "hierarchical", "params": {}},
        embedding_model="nvidia/NV-embed-V2",
        embedding_fingerprint=(
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ),
        embedding_dim=4000,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=owner.id,
        classification_level=level,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def test_collection_response_exposes_classification_level(client, db):
    owner = make_user(db, username="owner-r", role="user")
    coll = _make_collection(db, owner, level="機密")
    token = login(client, "owner-r")

    resp = client.get(
        f"/api/ingestion/collections/{coll.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["classification_level"] == "機密"


def test_non_admin_cannot_patch_classification_level(client, db):
    owner = make_user(db, username="owner-u", role="user")
    coll = _make_collection(db, owner, level="無機密")
    token = login(client, "owner-u")

    resp = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        json={"classification_level": "機密"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text
    db.refresh(coll)
    assert coll.classification_level == "無機密"


def test_admin_raise_writes_classification_event(client, db):
    owner = make_user(db, username="owner-a", role="user")
    make_user(db, username="admin-a", role="admin")
    coll = _make_collection(db, owner, level="無機密")
    assert coll.classification_event_id is None
    token = login(client, "admin-a")

    raise_resp = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        json={"classification_level": "機密"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert raise_resp.status_code == 200, raise_resp.text
    assert raise_resp.json()["classification_level"] == "機密"
    db.refresh(coll)
    assert coll.classification_level == "機密"
    assert coll.classification_source == "manual_admin"
    assert coll.classification_event_id is not None
    assert coll.classification_latched_at is not None

    events = (
        db.query(ClassificationEvent)
        .filter(
            ClassificationEvent.resource_type == "collection",
            ClassificationEvent.resource_id == str(coll.id),
        )
        .all()
    )
    assert len(events) == 1
    assert events[0].id == coll.classification_event_id
    assert events[0].previous_level == "無機密"
    assert events[0].new_level == "機密"
    assert events[0].reason == "manual_admin"


def test_admin_lower_rejected_with_declassification_flow_hint(client, db):
    owner = make_user(db, username="owner-l", role="user")
    make_user(db, username="admin-l", role="admin")
    coll = _make_collection(db, owner, level="極機密")
    token = login(client, "admin-l")

    lower_resp = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        json={"classification_level": "無機密"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert lower_resp.status_code == 409, lower_resp.text
    detail = lower_resp.json()["detail"]
    assert "不可直接降級" in detail
    assert "/api/classification/declassification-requests" in detail
    db.refresh(coll)
    assert coll.classification_level == "極機密"
    assert (
        db.query(ClassificationEvent)
        .filter(
            ClassificationEvent.resource_type == "collection",
            ClassificationEvent.resource_id == str(coll.id),
        )
        .count()
        == 0
    )


def test_admin_equal_level_is_noop_without_event(client, db):
    owner = make_user(db, username="owner-e", role="user")
    make_user(db, username="admin-e", role="admin")
    coll = _make_collection(db, owner, level="機密")
    token = login(client, "admin-e")

    resp = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        json={"classification_level": "機密"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["classification_level"] == "機密"
    db.refresh(coll)
    assert coll.classification_level == "機密"
    assert coll.classification_event_id is None
    assert (
        db.query(ClassificationEvent)
        .filter(
            ClassificationEvent.resource_type == "collection",
            ClassificationEvent.resource_id == str(coll.id),
        )
        .count()
        == 0
    )


def test_invalid_classification_level_rejected(client, db):
    owner = make_user(db, username="owner-b", role="user")
    make_user(db, username="admin-b", role="admin")
    coll = _make_collection(db, owner)
    token = login(client, "admin-b")

    resp = client.patch(
        f"/api/ingestion/collections/{coll.id}",
        json={"classification_level": "不存在的等級"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422, resp.text
