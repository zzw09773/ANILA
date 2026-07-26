"""Focused Gate 6 P5 trace-read compartment enforcement tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api import traces as trace_api
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    DocumentRequiredCompartment,
    SecurityCompartment,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.source_snapshot import SourceSnapshot
from app.models.trace_span import TraceSpan
from tests.conftest import make_user
from tests.test_proxy_task_wiring import _bearer, _jwt, _make_task


def _assert_denied_body(response, expected_detail: str) -> None:
    """W2-12:錯誤回應現在是 ``{"error": {...}, "detail": <legacy>}``。

    原本這裡寫 ``response.json() == {"detail": ...}``,逐字釘住整個 body。
    信封是**加法**改動,所以改成分開釘兩件事,斷言強度不減反增:

    - legacy ``detail`` 形狀與值逐字不變(過渡期雙寫的契約)。
    - 信封存在且 code 是 ``FORBIDDEN``(這些是 clearance/compartment 拒絕,
      走 status fallback —— W2-12 刻意沒有動 ``api/traces.py`` 的 raise site)。
    - top-level 只有 ``error`` 與 ``detail``,不得再冒出第三個欄位。
    """
    payload = response.json()
    assert payload["detail"] == expected_detail
    assert payload["error"]["code"] == "FORBIDDEN"
    assert payload["error"]["message"] == expected_detail
    assert "request_id" in payload["error"]
    assert set(payload) == {"error", "detail"}


def _seed_sourced_trace(
    db,
    *,
    subject,
    issuer,
    suffix: str,
    document_scope: bool = False,
    membership_granted: bool = True,
):
    collection = IngestionCollection(
        name=f"trace-source-{suffix}",
        chunking_config={},
        embedding_model="test-embed",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4000,
        created_by=subject.id,
        classification_level="機密",
    )
    db.add(collection)
    db.flush()
    document = None
    if document_scope:
        document = IngestionDocument(
            collection_id=collection.id,
            filename=f"trace-{suffix}.txt",
            sha256=f"{len(suffix):064x}",
            status="indexed",
            classification_level="機密",
        )
        db.add(document)
        db.flush()

    task = _make_task(db, subject)
    task.source_scope = "project"
    task.selected_collection_ids = [collection.id]
    task.classification_level = "機密"
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="document" if document is not None else "collection",
        source_scope="project",
        collection_ids=[collection.id],
        document_ids=[document.id] if document is not None else [],
        classification_level="機密",
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    db.add(
        TraceSpan(
            trace_id=task.trace_id,
            span_id=f"span-{suffix}",
            task_id=task.id,
            span_type="agent.retrieval.finished",
            name="retrieval",
            status="ok",
            producer="agent",
            classification_level="機密",
        )
    )
    now = datetime.now(timezone.utc)
    grant = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level="機密",
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        basis_ticket=f"P5-{suffix}",
        issued_by_user_id=issuer.id,
    )
    db.add(grant)
    db.flush()
    db.add(
        CollectionAccessGrant(
            clearance_grant_id=grant.id,
            collection_id=collection.id,
            membership_granted=membership_granted,
            need_to_know=True,
            basis_ticket=f"P5-{suffix}",
            issued_by_user_id=issuer.id,
        )
    )
    compartment = SecurityCompartment(
        code=f"P5_{suffix.upper()}",
        name=f"P5 {suffix}",
        created_by_user_id=issuer.id,
    )
    db.add(compartment)
    db.flush()
    requirement_type = (
        DocumentRequiredCompartment
        if document is not None
        else CollectionRequiredCompartment
    )
    requirement_kwargs = (
        {"document_id": document.id}
        if document is not None
        else {"collection_id": collection.id}
    )
    db.add(
        requirement_type(
            **requirement_kwargs,
            compartment_id=compartment.id,
            basis_ticket=f"P5-{suffix}",
            assigned_by_user_id=issuer.id,
        )
    )
    db.commit()
    return task, snapshot, collection, document, grant, compartment


def _grant_compartment(db, grant, compartment) -> None:
    db.add(
        ClearanceGrantCompartment(
            clearance_grant_id=grant.id,
            compartment_id=compartment.id,
        )
    )
    db.commit()


def _grant_classification(db, *, subject, issuer, level: str) -> ClearanceGrant:
    now = datetime.now(timezone.utc)
    grant = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level=level,
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        basis_ticket=f"P5-trace-read-{subject.username}",
        issued_by_user_id=issuer.id,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return grant


def test_collection_owner_role_cannot_bypass_required_compartment(client, db):
    issuer = make_user(db, username="p5_trace_collection_issuer", role="admin")
    owner = make_user(db, username="p5_trace_collection_owner", role="owner")
    task, _snapshot, _collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=owner,
        issuer=issuer,
        suffix="collection",
        membership_granted=False,
    )
    url = f"/api/traces/{task.trace_id}"

    denied = client.get(url, headers=_bearer(_jwt(owner)))
    assert denied.status_code == 403
    _assert_denied_body(denied, "trace clearance/compartment 拒絕")
    _grant_compartment(db, grant, compartment)
    assert client.get(url, headers=_bearer(_jwt(owner))).status_code == 200


def test_document_compartment_is_rederived_from_snapshot(client, db):
    issuer = make_user(db, username="p5_trace_document_issuer", role="admin")
    reader = make_user(db, username="p5_trace_document_reader")
    task, _snapshot, _collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=reader,
        issuer=issuer,
        suffix="document",
        document_scope=True,
    )
    url = f"/api/traces/{task.trace_id}"

    denied = client.get(url, headers=_bearer(_jwt(reader)))
    assert denied.status_code == 403
    _assert_denied_body(denied, "trace clearance/compartment 拒絕")
    _grant_compartment(db, grant, compartment)
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 200


def test_missing_or_cross_task_snapshot_association_fails_closed(client, db):
    issuer = make_user(db, username="p5_trace_binding_issuer", role="admin")
    reader = make_user(db, username="p5_trace_binding_reader")
    task, snapshot, _collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=reader,
        issuer=issuer,
        suffix="binding",
    )
    _grant_compartment(db, grant, compartment)
    url = f"/api/traces/{task.trace_id}"

    task.source_snapshot_id = None
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403

    other_task = _make_task(db, reader)
    snapshot.task_id = other_task.id
    task.source_snapshot_id = snapshot.id
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403

    snapshot.task_id = task.id
    snapshot.document_ids = [999999]
    task.source_snapshot_id = snapshot.id
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403


def test_source_scope_and_selected_collection_provenance_fail_closed(client, db):
    issuer = make_user(db, username="p5_trace_scope_issuer", role="admin")
    reader = make_user(db, username="p5_trace_scope_reader")
    task, snapshot, collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=reader,
        issuer=issuer,
        suffix="scope",
    )
    _grant_compartment(db, grant, compartment)
    url = f"/api/traces/{task.trace_id}"
    headers = _bearer(_jwt(reader))

    assert client.get(url, headers=headers).status_code == 200

    snapshot.source_scope = "personal"
    db.commit()
    response = client.get(url, headers=headers)
    assert response.status_code == 403
    _assert_denied_body(response, "trace Task/Snapshot source scope 不符")

    snapshot.source_scope = task.source_scope
    db.commit()
    assert client.get(url, headers=headers).status_code == 200

    snapshot.collection_ids = [collection.id + 999999]
    db.commit()
    response = client.get(url, headers=headers)
    assert response.status_code == 403
    _assert_denied_body(response, "trace Snapshot collection scope 不符")


def test_trace_span_classification_requires_matching_clearance_ceiling(client, db):
    issuer = make_user(db, username="p5_trace_level_issuer", role="admin")
    reader = make_user(db, username="p5_trace_level_reader")
    task, _snapshot, _collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=reader,
        issuer=issuer,
        suffix="level",
    )
    _grant_compartment(db, grant, compartment)
    span = db.query(TraceSpan).filter(TraceSpan.trace_id == task.trace_id).one()
    span.classification_level = "絕對機密"
    db.commit()
    url = f"/api/traces/{task.trace_id}"

    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403

    grant.max_classification_level = "絕對機密"
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 200


def test_task_owned_trace_rejects_null_and_cross_task_span_bindings(client, db):
    issuer = make_user(db, username="p5_trace_span_binding_issuer", role="admin")
    reader = make_user(db, username="p5_trace_span_binding_reader")
    task, _snapshot, _collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=reader,
        issuer=issuer,
        suffix="span_binding",
    )
    _grant_compartment(db, grant, compartment)
    span = db.query(TraceSpan).filter(TraceSpan.trace_id == task.trace_id).one()
    other_task = _make_task(db, reader)
    url = f"/api/traces/{task.trace_id}"

    span.task_id = other_task.id
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403

    span.task_id = None
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403


def test_empty_snapshot_requires_canonical_none_scope_and_origin(client, db):
    issuer = make_user(db, username="p5_trace_empty_issuer", role="admin")
    reader = make_user(db, username="p5_trace_empty_reader")
    task = _make_task(db, reader)
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="none",
        source_scope="none",
        collection_ids=[],
        document_ids=[],
        classification_level="機密",
    )
    db.add(snapshot)
    db.flush()
    task.source_scope = "none"
    task.source_snapshot_id = snapshot.id
    db.add(
        TraceSpan(
            trace_id=task.trace_id,
            span_id="span-empty",
            task_id=task.id,
            span_type="agent.run.finished",
            name="empty",
            status="ok",
            producer="agent",
            classification_level="無機密",
        )
    )
    _grant_classification(
        db,
        subject=reader,
        issuer=issuer,
        level="機密",
    )
    db.commit()
    url = f"/api/traces/{task.trace_id}"
    headers = _bearer(_jwt(reader))

    assert client.get(url, headers=headers).status_code == 200

    snapshot.source_scope = "project"
    db.commit()
    response = client.get(url, headers=headers)
    assert response.status_code == 403
    _assert_denied_body(response, "trace Task/Snapshot source scope 不符")

    snapshot.source_scope = "none"
    db.commit()
    assert client.get(url, headers=headers).status_code == 200

    snapshot.origin = "collection"
    db.commit()
    response = client.get(url, headers=headers)
    assert response.status_code == 403
    _assert_denied_body(response, "trace Snapshot source scope 不完整")


def test_admin_task_bypass_does_not_bypass_data_clearance(client, db):
    issuer = make_user(db, username="p5_trace_admin_issuer", role="owner")
    requester = make_user(db, username="p5_trace_admin_requester")
    task, _snapshot, collection, _document, _grant, compartment = _seed_sourced_trace(
        db,
        subject=requester,
        issuer=issuer,
        suffix="admin",
    )
    admin = make_user(db, username="p5_trace_admin_reader", role="admin")
    url = f"/api/traces/{task.trace_id}"

    assert client.get(url, headers=_bearer(_jwt(admin))).status_code == 403

    now = datetime.now(timezone.utc)
    admin_grant = ClearanceGrant(
        subject_user_id=admin.id,
        max_classification_level="機密",
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        basis_ticket="P5-admin-read",
        issued_by_user_id=issuer.id,
    )
    db.add(admin_grant)
    db.flush()
    db.add_all(
        [
            CollectionAccessGrant(
                clearance_grant_id=admin_grant.id,
                collection_id=collection.id,
                membership_granted=True,
                need_to_know=True,
                basis_ticket="P5-admin-read",
                issued_by_user_id=issuer.id,
            ),
            ClearanceGrantCompartment(
                clearance_grant_id=admin_grant.id,
                compartment_id=compartment.id,
            ),
        ]
    )
    db.commit()

    assert client.get(url, headers=_bearer(_jwt(admin))).status_code == 200


def test_no_source_trace_requires_active_clearance_for_task_and_span_floor(client, db):
    issuer = make_user(db, username="p5_trace_none_issuer", role="admin")
    reader = make_user(db, username="p5_trace_none_reader")
    task = _make_task(db, reader)
    task.source_scope = "none"
    task.selected_collection_ids = []
    task.classification_level = "機密"
    db.add(
        TraceSpan(
            trace_id=task.trace_id,
            span_id="span-none-high",
            task_id=task.id,
            span_type="agent.run.finished",
            name="none-high",
            status="ok",
            producer="agent",
            classification_level="絕對機密",
        )
    )
    db.commit()
    url = f"/api/traces/{task.trace_id}"

    denied = client.get(url, headers=_bearer(_jwt(reader)))
    assert denied.status_code == 403
    assert denied.json()["detail"] == "trace classification clearance 拒絕"
    assert task.trace_id not in denied.text
    assert "絕對機密" not in denied.text

    grant = _grant_classification(
        db,
        subject=reader,
        issuer=issuer,
        level="機密",
    )
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403

    now = datetime.now(timezone.utc)
    grant.max_classification_level = "絕對機密"
    grant.valid_from = now - timedelta(hours=2)
    grant.expires_at = now - timedelta(hours=1)
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403

    grant.expires_at = now + timedelta(hours=1)
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 200


def test_producer_raised_span_classification_is_enforced_on_no_source_read(client, db):
    issuer = make_user(db, username="p5_trace_raise_issuer", role="admin")
    reader = make_user(db, username="p5_trace_raise_reader")
    task = _make_task(db, reader)
    task.source_scope = "none"
    task.selected_collection_ids = []
    task.classification_level = "無機密"
    db.commit()

    ingest = client.post(
        f"/v1/traces/{task.trace_id}/spans",
        headers=_bearer(_jwt(reader)),
        json={
            "spans": [
                {
                    "span_id": "producer-raised",
                    "span_type": "agent.run.finished",
                    "name": "producer-raised",
                    "classification_level": "絕對機密",
                }
            ]
        },
    )
    assert ingest.status_code == 202
    url = f"/api/traces/{task.trace_id}"

    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403
    grant = _grant_classification(
        db,
        subject=reader,
        issuer=issuer,
        level="機密",
    )
    assert client.get(url, headers=_bearer(_jwt(reader))).status_code == 403
    grant.max_classification_level = "絕對機密"
    db.commit()

    response = client.get(url, headers=_bearer(_jwt(reader)))
    assert response.status_code == 200
    assert response.json()["spans"][0]["classification_level"] == "絕對機密"


def test_orphan_trace_admin_requires_span_floor_and_null_task_bindings(client, db):
    issuer = make_user(db, username="p5_trace_orphan_issuer", role="owner")
    admin = make_user(db, username="p5_trace_orphan_admin", role="admin")
    plain = make_user(db, username="p5_trace_orphan_plain")
    db.add(
        TraceSpan(
            trace_id="p5-orphan-high",
            span_id="orphan-high",
            task_id=None,
            span_type="legacy.runtime",
            name="orphan-high",
            status="ok",
            producer="proxy",
            classification_level="極機密",
        )
    )
    db.commit()
    url = "/api/traces/p5-orphan-high"

    assert client.get(url, headers=_bearer(_jwt(plain))).status_code == 403
    denied = client.get(url, headers=_bearer(_jwt(admin)))
    assert denied.status_code == 403
    assert denied.json()["detail"] == "trace classification clearance 拒絕"

    grant = _grant_classification(
        db,
        subject=admin,
        issuer=issuer,
        level="機密",
    )
    assert client.get(url, headers=_bearer(_jwt(admin))).status_code == 403
    grant.max_classification_level = "極機密"
    db.commit()
    assert client.get(url, headers=_bearer(_jwt(admin))).status_code == 200

    task = _make_task(db, plain)
    db.add(
        TraceSpan(
            trace_id="p5-orphan-bound",
            span_id="orphan-bound",
            task_id=task.id,
            span_type="legacy.runtime",
            name="orphan-bound",
            status="ok",
            producer="proxy",
            classification_level="無機密",
        )
    )
    db.commit()
    bound = client.get(
        "/api/traces/p5-orphan-bound",
        headers=_bearer(_jwt(admin)),
    )
    assert bound.status_code == 403
    assert bound.json()["detail"] == "taskless trace span binding 不符"


def test_sourced_trace_uses_one_batch_clearance_call_for_all_documents(
    client, db, monkeypatch
):
    issuer = make_user(db, username="p5_trace_batch_issuer", role="admin")
    reader = make_user(db, username="p5_trace_batch_reader")
    task, snapshot, collection, _document, grant, compartment = _seed_sourced_trace(
        db,
        subject=reader,
        issuer=issuer,
        suffix="batch",
        document_scope=True,
    )
    _grant_compartment(db, grant, compartment)
    extra_documents = []
    for index in range(4):
        document = IngestionDocument(
            collection_id=collection.id,
            filename=f"trace-batch-{index}.txt",
            sha256=f"{1000 + index:064x}",
            status="indexed",
            classification_level="機密",
        )
        db.add(document)
        extra_documents.append(document)
    db.flush()
    snapshot.document_ids = [
        *snapshot.document_ids,
        *(document.id for document in extra_documents),
    ]
    db.commit()

    calls: list[tuple[list[int], list[int]]] = []
    original_batch = trace_api.resolve_and_evaluate_data_access_batch

    def batch_spy(*args, **kwargs):
        calls.append((kwargs["collection_ids"], kwargs["document_ids"]))
        return original_batch(*args, **kwargs)

    monkeypatch.setattr(
        trace_api,
        "resolve_and_evaluate_data_access_batch",
        batch_spy,
    )

    response = client.get(
        f"/api/traces/{task.trace_id}",
        headers=_bearer(_jwt(reader)),
    )

    assert response.status_code == 200
    assert calls == [([collection.id], sorted(snapshot.document_ids))]
