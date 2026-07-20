"""HTTP regressions for Gate 2 document-data clearance boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.clearance import (
    DocumentRequiredCompartment,
    SecurityCompartment,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.modules.clearance.service import (
    grant_collection_access,
    issue_clearance_grant,
)
from tests.conftest import login, make_user


def _governed_document(db):
    owner = make_user(db, username="boundary-owner", role="developer")
    manager = make_user(db, username="boundary-manager", role="admin")
    collection = IngestionCollection(
        name="Restricted collection",
        chunking_config={"strategy": "fixed"},
        embedding_model="embed",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4,
        status="active",
        created_by=owner.id,
        classification_level="機密",
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    document = IngestionDocument(
        collection_id=collection.id,
        filename="restricted.txt",
        sha256="d" * 64,
        status="indexed",
        classification_level="機密",
        storage_path="does-not-matter-before-clearance",
    )
    compartment = SecurityCompartment(
        code="PROJECT_X",
        name="Project X",
        is_active=True,
        created_by_user_id=manager.id,
    )
    db.add_all([document, compartment])
    db.commit()
    db.refresh(document)
    db.refresh(compartment)

    now = datetime.now(timezone.utc)
    grant = issue_clearance_grant(
        db,
        actor=manager,
        subject_user_id=owner.id,
        max_classification_level="絕對機密",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        basis_ticket="BOUNDARY-CLEARANCE",
    )
    grant_collection_access(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        collection_id=collection.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="BOUNDARY-NTK",
    )
    # Deliberately do not add this compartment to the clearance grant.  The
    # owner passes the old management ACL but must fail canonical data access.
    db.add(
        DocumentRequiredCompartment(
            document_id=document.id,
            compartment_id=compartment.id,
            basis_ticket="BOUNDARY-REQUIREMENT",
            assigned_by_user_id=manager.id,
        )
    )
    db.commit()
    return owner, collection, document


@pytest.mark.parametrize(
    "path",
    [
        "/api/ingestion/documents/{document_id}/chunks",
        "/api/ingestion/documents/{document_id}/chunks/1/embedding-debug",
        "/api/ingestion/documents/{document_id}/blob",
    ],
)
def test_document_derivatives_require_canonical_clearance(client, db, path):
    owner, _collection, document = _governed_document(db)
    token = login(client, owner.username)

    response = client.get(
        path.format(document_id=document.id),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "compartment" in response.json()["detail"]


def test_eval_run_rejects_sample_without_document_clearance(client, db):
    owner, collection, document = _governed_document(db)
    token = login(client, owner.username)

    response = client.post(
        "/api/ingestion/eval-runs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "collection_id": collection.id,
            "name": "must not enqueue",
            "sample_document_ids": [document.id],
            "strategies_tried": [{"name": "fixed", "params": {}}],
            "queries": [{"query": "q", "expected_doc_id": document.id}],
        },
    )

    assert response.status_code == 403
    assert "clearance" in response.json()["detail"]
