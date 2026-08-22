from datetime import datetime, timezone

from app.models.audit_log import AuditLog
from app.models.ingestion import IngestionCollection, IngestionDocument
from tests.conftest import login, make_user


def _headers(client, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, username)}"}


def test_audit_range_pagination_and_total_header(client, db):
    make_user(db, username="audit-admin", role="admin")
    for day in (1, 2, 3):
        db.add(
            AuditLog(
                actor_username="operator",
                action="review-pass",
                resource_type="test",
                status="success",
                created_at=datetime(2026, 8, day, 8, tzinfo=timezone.utc),
            )
        )
    db.commit()

    response = client.get(
        "/api/audit-logs",
        params={
            "action": "review-pass",
            "since": "2026-08-01T00:00:00Z",
            "until": "2026-08-03T23:59:59Z",
            "limit": 1,
            "offset": 1,
        },
        headers=_headers(client, "audit-admin"),
    )

    assert response.status_code == 200, response.text
    assert response.headers["x-total-count"] == "3"
    assert len(response.json()) == 1
    assert response.json()[0]["created_at"].startswith("2026-08-02")


def test_collection_response_count_matches_opened_collection(client, db):
    owner = make_user(db, username="collection-owner")
    collection = IngestionCollection(
        name="review-pass-count",
        chunking_config={"strategy": "fixed"},
        embedding_model="fixture-embedding",
        embedding_dim=8,
        created_by=owner.id,
        document_count=0,  # deliberately stale denormalized counter
        origin="anilalm",
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)

    for index, status in enumerate(("indexed", "failed"), start=1):
        db.add(
            IngestionDocument(
                collection_id=collection.id,
                filename=f"source-{index}.pdf",
                sha256=f"{index:064d}",
                status=status,
            )
        )
    db.commit()

    headers = _headers(client, "collection-owner")
    listed = client.get(
        "/api/ingestion/collections?origin=anilalm",
        headers=headers,
    )
    detail = client.get(
        f"/api/ingestion/collections/{collection.id}",
        headers=headers,
    )

    assert listed.status_code == 200, listed.text
    assert detail.status_code == 200, detail.text
    assert listed.json()[0]["document_count"] == 2
    assert detail.json()["document_count"] == 2
