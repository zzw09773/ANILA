# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance model, evaluator, audit, and management API tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from anila_contracts import Classification
from app.models.audit_log import AuditLog
from app.models.clearance import (
    ClearanceGrant,
    CollectionAccessGrant,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.modules.clearance import service as clearance_service
from app.modules.clearance.service import (
    ClearancePolicyDataError,
    _classification_from_storage,
    add_grant_compartment,
    assign_collection_required_compartment,
    assign_document_required_compartment,
    create_security_compartment,
    grant_collection_access,
    issue_clearance_grant,
    resolve_and_evaluate_data_access,
    resolve_and_evaluate_data_access_batch,
    resolve_data_access_context,
    revoke_clearance_grant,
)
from tests.conftest import make_user

NOW = datetime(2026, 7, 12, 6, 0, 0, tzinfo=timezone.utc)
LEVELS = list(Classification)


def _collection(db, owner, *, level: Classification) -> IngestionCollection:
    row = IngestionCollection(
        name=f"collection-{owner.id}-{level.rank}",
        description=None,
        chunking_config={},
        embedding_model="test-embed",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4000,
        created_by=owner.id,
        classification_level=level.to_storage(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _document(
    db, collection: IngestionCollection, *, level: Classification, salt: int = 1
) -> IngestionDocument:
    row = IngestionDocument(
        collection_id=collection.id,
        filename=f"document-{salt}.txt",
        sha256=f"{salt:064x}",
        status="indexed",
        classification_level=level.to_storage(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _active_grant(
    db,
    *,
    manager,
    subject,
    level: Classification,
    valid_from: datetime | None = None,
    expires_at: datetime | None = None,
):
    return issue_clearance_grant(
        db,
        actor=manager,
        subject_user_id=subject.id,
        max_classification_level=level,
        valid_from=valid_from or NOW - timedelta(hours=1),
        expires_at=expires_at or NOW + timedelta(hours=1),
        basis_ticket="SEC-2026-001",
    )


def _collection_access(
    db,
    *,
    manager,
    grant,
    collection,
    membership: bool = True,
    need_to_know: bool = True,
):
    return grant_collection_access(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        collection_id=collection.id,
        membership_granted=membership,
        need_to_know=need_to_know,
        basis_ticket="NTK-2026-001",
    )


@pytest.mark.parametrize("grant_level", LEVELS)
@pytest.mark.parametrize("required_level", LEVELS)
def test_five_level_boundary_requires_grant_at_or_above_resource(
    db, grant_level: Classification, required_level: Classification
) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=required_level)
    grant = _active_grant(db, manager=manager, subject=subject, level=grant_level)
    _collection_access(db, manager=manager, grant=grant, collection=collection)

    decision = resolve_and_evaluate_data_access(
        db,
        user_id=subject.id,
        collection_id=collection.id,
        now=NOW,
    )

    assert decision.allowed is (grant_level.rank >= required_level.rank)
    assert decision.authorized_classification is (
        grant_level if decision.allowed else None
    )


def test_same_level_different_compartment_is_denied(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.CONFIDENTIAL)
    required = create_security_compartment(
        db, actor=manager, code="PROJECT_A", name="Project A"
    )
    other = create_security_compartment(
        db, actor=manager, code="PROJECT_B", name="Project B"
    )
    assign_collection_required_compartment(
        db,
        actor=manager,
        collection_id=collection.id,
        compartment_id=required.id,
        basis_ticket="REQ-A",
    )
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.CONFIDENTIAL
    )
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=other.id,
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is False


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_platform_role_without_clearance_never_bypasses_data_policy(
    db, role: str
) -> None:
    subject = make_user(db, f"subject-{role}", role=role)
    data_owner = make_user(db, "data-owner")
    collection = _collection(db, data_owner, level=Classification.UNCLASSIFIED)

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is False
    assert decision.reason_code == "no_active_clearance_grant"


def test_collection_owner_only_substitutes_membership_not_compartment(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject", role="owner")
    collection = _collection(db, subject, level=Classification.TRADE_SECRET)
    compartment = create_security_compartment(
        db, actor=manager, code="OWNER_SCOPE", name="Owner Scope"
    )
    assign_collection_required_compartment(
        db,
        actor=manager,
        collection_id=collection.id,
        compartment_id=compartment.id,
        basis_ticket="REQ-OWNER",
    )
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TRADE_SECRET
    )
    _collection_access(
        db,
        manager=manager,
        grant=grant,
        collection=collection,
        membership=False,
        need_to_know=True,
    )

    denied = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert denied.allowed is False

    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=compartment.id,
    )
    allowed = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert allowed.allowed is True


def test_forged_collection_owner_context_is_rejected(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    _collection_access(
        db,
        manager=manager,
        grant=grant,
        collection=collection,
        membership=False,
        need_to_know=True,
    )
    context = resolve_data_access_context(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    forged = replace(context, is_collection_owner=True)

    from app.modules.clearance.service import evaluate_data_access

    with pytest.raises(ClearancePolicyDataError, match="authoritative"):
        evaluate_data_access(db, context=forged)


@pytest.mark.parametrize("state", ["expired", "future", "revoked"])
def test_expired_future_and_revoked_grants_deny(db, state: str) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    windows = {
        "expired": (NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
        "future": (NOW + timedelta(hours=1), NOW + timedelta(hours=2)),
        "revoked": (NOW - timedelta(hours=1), NOW + timedelta(hours=1)),
    }
    grant = _active_grant(
        db,
        manager=manager,
        subject=subject,
        level=Classification.TOP_SECRET,
        valid_from=windows[state][0],
        expires_at=windows[state][1],
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)
    if state == "revoked":
        revoke_clearance_grant(db, actor=manager, clearance_grant_id=grant.id, now=NOW)

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is False
    assert decision.reason_code == "no_active_clearance_grant"


def test_collection_membership_without_need_to_know_denies(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    _collection_access(
        db,
        manager=manager,
        grant=grant,
        collection=collection,
        membership=True,
        need_to_know=False,
    )

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is False


def test_authority_cannot_be_composed_across_clearance_grants(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.CONFIDENTIAL)
    compartment = create_security_compartment(
        db, actor=manager, code="SILO_A", name="Silo A"
    )
    assign_collection_required_compartment(
        db,
        actor=manager,
        collection_id=collection.id,
        compartment_id=compartment.id,
        basis_ticket="REQ-SILO",
    )

    high_grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TOP_SECRET
    )
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=high_grant.id,
        compartment_id=compartment.id,
    )
    access_grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    _collection_access(db, manager=manager, grant=access_grant, collection=collection)

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is False
    assert decision.reason_code == "no_single_grant_satisfies_requirements"


def test_highest_complete_single_grant_sets_chunk_retrieval_ceiling(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    low = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TRADE_SECRET
    )
    high = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TOP_SECRET
    )
    _collection_access(db, manager=manager, grant=low, collection=collection)
    _collection_access(db, manager=manager, grant=high, collection=collection)

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is True
    assert decision.clearance_grant_id == high.id
    assert decision.authorized_classification is Classification.TOP_SECRET


def test_document_effective_requirements_union_collection_and_document(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.CONFIDENTIAL)
    document = _document(db, collection, level=Classification.UNCLASSIFIED)
    collection_compartment = create_security_compartment(
        db, actor=manager, code="COLL_A", name="Collection A"
    )
    document_compartment = create_security_compartment(
        db, actor=manager, code="DOC_B", name="Document B"
    )
    assign_collection_required_compartment(
        db,
        actor=manager,
        collection_id=collection.id,
        compartment_id=collection_compartment.id,
        basis_ticket="REQ-COLL",
    )
    assign_document_required_compartment(
        db,
        actor=manager,
        document_id=document.id,
        compartment_id=document_compartment.id,
        basis_ticket="REQ-DOC",
    )
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.CONFIDENTIAL
    )
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=collection_compartment.id,
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)

    context = resolve_data_access_context(
        db,
        user_id=subject.id,
        collection_id=collection.id,
        document_id=document.id,
        now=NOW,
    )
    assert context.required_classification is Classification.CONFIDENTIAL
    assert context.required_compartment_ids == frozenset(
        {collection_compartment.id, document_compartment.id}
    )
    with pytest.raises(FrozenInstanceError):
        context.collection_id = 999  # type: ignore[misc]
    assert (
        resolve_and_evaluate_data_access(
            db,
            user_id=subject.id,
            collection_id=collection.id,
            document_id=document.id,
            now=NOW,
        ).allowed
        is False
    )

    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=document_compartment.id,
    )
    assert (
        resolve_and_evaluate_data_access(
            db,
            user_id=subject.id,
            collection_id=collection.id,
            document_id=document.id,
            now=NOW,
        ).allowed
        is True
    )


def test_batch_decisions_match_single_collection_and_document_decisions(db) -> None:
    manager = make_user(db, "batch-manager", role="admin")
    subject = make_user(db, "batch-subject")
    owner = make_user(db, "batch-owner")
    collection = _collection(db, owner, level=Classification.CONFIDENTIAL)
    documents = [
        _document(db, collection, level=level, salt=index + 100)
        for index, level in enumerate(
            [Classification.UNCLASSIFIED, Classification.SECRET]
        )
    ]
    compartment = create_security_compartment(
        db, actor=manager, code="BATCH_SCOPE", name="Batch scope"
    )
    assign_document_required_compartment(
        db,
        actor=manager,
        document_id=documents[1].id,
        compartment_id=compartment.id,
        basis_ticket="BATCH-REQ",
    )
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TOP_SECRET
    )
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=compartment.id,
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)

    singles = {
        (collection.id, document_id): resolve_and_evaluate_data_access(
            db,
            user_id=subject.id,
            collection_id=collection.id,
            document_id=document_id,
            now=NOW,
        )
        for document_id in [None, *(document.id for document in documents)]
    }
    batch = resolve_and_evaluate_data_access_batch(
        db,
        user_id=subject.id,
        collection_ids=[collection.id],
        document_ids=[document.id for document in documents],
        now=NOW,
    )

    assert set(batch) == set(singles)
    for key, decision in batch.items():
        single = singles[key]
        assert decision.allowed is single.allowed
        assert decision.reason_code == single.reason_code
        assert decision.authorized_classification is single.authorized_classification
        assert decision.clearance_grant_id == single.clearance_grant_id
        assert decision.context == single.context


@pytest.mark.parametrize("denial", ["no_grant", "missing_membership", "compartment"])
def test_batch_denials_match_single_evaluator(db, denial: str) -> None:
    manager = make_user(db, f"batch-deny-manager-{denial}", role="admin")
    subject = make_user(db, f"batch-deny-subject-{denial}")
    owner = make_user(db, f"batch-deny-owner-{denial}")
    collection = _collection(db, owner, level=Classification.CONFIDENTIAL)
    document = _document(db, collection, level=Classification.CONFIDENTIAL, salt=200)

    if denial != "no_grant":
        grant = _active_grant(
            db, manager=manager, subject=subject, level=Classification.TOP_SECRET
        )
        if denial != "missing_membership":
            _collection_access(db, manager=manager, grant=grant, collection=collection)
        if denial == "compartment":
            required = create_security_compartment(
                db,
                actor=manager,
                code="BATCH_DENY",
                name="Batch deny",
            )
            assign_document_required_compartment(
                db,
                actor=manager,
                document_id=document.id,
                compartment_id=required.id,
                basis_ticket="BATCH-DENY",
            )

    single = resolve_and_evaluate_data_access(
        db,
        user_id=subject.id,
        collection_id=collection.id,
        document_id=document.id,
        now=NOW,
    )
    batch = resolve_and_evaluate_data_access_batch(
        db,
        user_id=subject.id,
        collection_ids=[collection.id],
        document_ids=[document.id],
        now=NOW,
    )[(collection.id, document.id)]

    assert batch.allowed is False
    assert batch.reason_code == single.reason_code
    assert batch.authorized_classification is single.authorized_classification


def test_batch_rejects_document_collection_mismatch_like_single(db) -> None:
    owner = make_user(db, "batch-mismatch-owner")
    first = _collection(db, owner, level=Classification.UNCLASSIFIED)
    second = _collection(db, owner, level=Classification.UNCLASSIFIED)
    document = _document(db, second, level=Classification.UNCLASSIFIED, salt=300)

    with pytest.raises(LookupError, match="document"):
        resolve_and_evaluate_data_access(
            db,
            user_id=owner.id,
            collection_id=first.id,
            document_id=document.id,
            now=NOW,
        )
    with pytest.raises(LookupError, match="document"):
        resolve_and_evaluate_data_access_batch(
            db,
            user_id=owner.id,
            collection_ids=[first.id],
            document_ids=[document.id],
            now=NOW,
        )


def test_batch_inactive_and_corrupt_grants_fail_closed_like_single(db) -> None:
    manager = make_user(db, "batch-corrupt-manager", role="admin")
    subject = make_user(db, "batch-corrupt-subject")
    owner = make_user(db, "batch-corrupt-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TOP_SECRET
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)

    subject.is_active = False
    db.commit()
    single = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    batch = resolve_and_evaluate_data_access_batch(
        db,
        user_id=subject.id,
        collection_ids=[collection.id],
        document_ids=[],
        now=NOW,
    )[(collection.id, None)]
    assert single.reason_code == batch.reason_code == "subject_inactive"

    subject.is_active = True
    db.commit()
    grant.max_classification_level = "公開"
    with db.no_autoflush:
        with pytest.raises(ClearancePolicyDataError, match="未知 classification"):
            resolve_and_evaluate_data_access(
                db, user_id=subject.id, collection_id=collection.id, now=NOW
            )
        with pytest.raises(ClearancePolicyDataError, match="未知 classification"):
            resolve_and_evaluate_data_access_batch(
                db,
                user_id=subject.id,
                collection_ids=[collection.id],
                document_ids=[],
                now=NOW,
            )


def test_batch_query_count_is_constant_and_every_authority_read_is_locked(
    db, monkeypatch
) -> None:
    manager = make_user(db, "batch-query-manager", role="admin")
    subject = make_user(db, "batch-query-subject")
    owner = make_user(db, "batch-query-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    documents = [
        _document(
            db,
            collection,
            level=Classification.UNCLASSIFIED,
            salt=400 + index,
        )
        for index in range(5)
    ]
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    compartment = create_security_compartment(
        db,
        actor=manager,
        code="BATCH_LOCK_ORDER",
        name="Batch lock order",
        description=None,
    )
    assign_collection_required_compartment(
        db,
        actor=manager,
        collection_id=collection.id,
        compartment_id=compartment.id,
        basis_ticket="BATCH-LOCK-COLLECTION",
    )
    assign_document_required_compartment(
        db,
        actor=manager,
        document_id=documents[0].id,
        compartment_id=compartment.id,
        basis_ticket="BATCH-LOCK-DOCUMENT",
    )
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=compartment.id,
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)
    subject_id = subject.id
    collection_id = collection.id
    document_ids = [document.id for document in documents]

    lock_calls: list[dict] = []
    original_with_for_update = Query.with_for_update

    def lock_spy(query, *args, **kwargs):
        lock_calls.append(dict(kwargs))
        return original_with_for_update(query, *args, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", lock_spy)

    def statements_for(document_subset) -> list[str]:
        statements: list[str] = []

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(" ".join(statement.lower().split()))

        sqlalchemy_event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            resolve_and_evaluate_data_access_batch(
                db,
                user_id=subject_id,
                collection_ids=[collection_id],
                document_ids=document_subset,
                now=NOW,
            )
        finally:
            sqlalchemy_event.remove(db.get_bind(), "before_cursor_execute", record)
        return statements

    single_statements: list[str] = []

    def record_single(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            single_statements.append(" ".join(statement.lower().split()))

    sqlalchemy_event.listen(db.get_bind(), "before_cursor_execute", record_single)
    try:
        single_context = resolve_data_access_context(
            db,
            user_id=subject_id,
            collection_id=collection_id,
            document_id=document_ids[0],
            now=NOW,
        )
    finally:
        sqlalchemy_event.remove(db.get_bind(), "before_cursor_execute", record_single)
    single_lock_calls = list(lock_calls)
    lock_calls.clear()

    assert single_context.required_compartment_ids == frozenset({compartment.id})
    assert single_lock_calls and all(
        call.get("read") is True for call in single_lock_calls
    )
    single_expected_ordering = {
        "collection_required_compartments": (
            "order by collection_required_compartments.collection_id asc, "
            "collection_required_compartments.compartment_id asc"
        ),
        "document_required_compartments": (
            "order by document_required_compartments.document_id asc, "
            "document_required_compartments.compartment_id asc"
        ),
        "security_compartments": "order by security_compartments.id asc",
    }
    for table, order_clause in single_expected_ordering.items():
        matching = [
            statement for statement in single_statements if f" {table}" in statement
        ]
        assert len(matching) == 1
        assert order_clause in matching[0]

    one = statements_for(document_ids[:1])
    one_lock_count = len(lock_calls)
    lock_calls.clear()
    five = statements_for(document_ids)
    five_lock_count = len(lock_calls)

    assert len(one) == len(five)
    assert one_lock_count == five_lock_count
    assert lock_calls and all(call.get("read") is True for call in lock_calls)
    tables = (
        "users",
        "clearance_grants",
        "ingestion_collections",
        "ingestion_documents",
        "collection_required_compartments",
        "document_required_compartments",
        "security_compartments",
        "clearance_grant_compartments",
        "collection_access_grants",
    )
    for table in tables:
        assert sum(f" {table}" in statement for statement in one) == 1
        assert sum(f" {table}" in statement for statement in five) == 1

    expected_ordering = {
        "clearance_grants": "order by clearance_grants.id asc",
        "ingestion_collections": "order by ingestion_collections.id asc",
        "ingestion_documents": "order by ingestion_documents.id asc",
        "collection_required_compartments": (
            "order by collection_required_compartments.collection_id asc, "
            "collection_required_compartments.compartment_id asc"
        ),
        "document_required_compartments": (
            "order by document_required_compartments.document_id asc, "
            "document_required_compartments.compartment_id asc"
        ),
        "security_compartments": "order by security_compartments.id asc",
        "clearance_grant_compartments": (
            "order by clearance_grant_compartments.clearance_grant_id asc, "
            "clearance_grant_compartments.compartment_id asc"
        ),
        "collection_access_grants": "order by collection_access_grants.id asc",
    }
    for table, order_clause in expected_ordering.items():
        one_statement = next(statement for statement in one if f" {table}" in statement)
        five_statement = next(
            statement for statement in five if f" {table}" in statement
        )
        assert order_clause in one_statement
        assert order_clause in five_statement


def test_governance_multirow_locks_follow_resource_before_authority_order(
    db, monkeypatch
) -> None:
    """Write locks must match runtime collection/compartment-before-grant order."""

    manager = make_user(db, "lock-order-manager", role="admin")
    subject = make_user(db, "lock-order-subject")
    owner = make_user(db, "lock-order-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    compartment = create_security_compartment(
        db,
        actor=manager,
        code="LOCK_ORDER_COMPARTMENT",
        name="Lock order compartment",
    )

    locked_models: list[str] = []
    original_for_update_get = clearance_service._for_update_get

    def lock_spy(session, model, primary_key):
        locked_models.append(model.__tablename__)
        return original_for_update_get(session, model, primary_key)

    monkeypatch.setattr(clearance_service, "_for_update_get", lock_spy)

    grant_collection_access(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        collection_id=collection.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="LOCK-ORDER-COLLECTION",
    )
    assert locked_models == ["ingestion_collections", "clearance_grants"]

    locked_models.clear()
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=compartment.id,
    )
    assert locked_models == ["security_compartments", "clearance_grants"]


def test_unknown_and_null_classification_fail_closed(db) -> None:
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    collection.classification_level = "公開"
    db.commit()
    with pytest.raises(ClearancePolicyDataError, match="未知 classification"):
        resolve_data_access_context(
            db,
            user_id=owner.id,
            collection_id=collection.id,
            now=NOW,
        )
    with pytest.raises(ClearancePolicyDataError, match="NULL"):
        _classification_from_storage(None, field_name="unit")


def test_inactive_required_compartment_is_policy_corruption_not_a_bypass(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    compartment = create_security_compartment(
        db, actor=manager, code="ACTIVE_SCOPE", name="Active scope"
    )
    assign_collection_required_compartment(
        db,
        actor=manager,
        collection_id=collection.id,
        compartment_id=compartment.id,
        basis_ticket="REQ-ACTIVE",
    )
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.TOP_SECRET
    )
    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=compartment.id,
    )
    _collection_access(db, manager=manager, grant=grant, collection=collection)
    compartment.is_active = False
    db.commit()

    with pytest.raises(ClearancePolicyDataError, match="已停用"):
        resolve_and_evaluate_data_access(
            db,
            user_id=subject.id,
            collection_id=collection.id,
            now=NOW,
        )


def test_regular_user_cannot_call_management_service(db) -> None:
    actor = make_user(db, "ordinary")
    subject = make_user(db, "subject")
    with pytest.raises(PermissionError):
        _active_grant(
            db,
            manager=actor,
            subject=subject,
            level=Classification.UNCLASSIFIED,
        )


def test_inactive_admin_cannot_call_management_service(db) -> None:
    actor = make_user(db, "inactive-admin", role="admin")
    actor.is_active = False
    subject = make_user(db, "inactive-admin-subject")
    db.commit()

    with pytest.raises(PermissionError, match="admin/owner"):
        _active_grant(
            db,
            manager=actor,
            subject=subject,
            level=Classification.UNCLASSIFIED,
        )


@pytest.mark.parametrize("code", ["PROJECT/X", "PROJECT X", "PROJECT\tX", "X" * 65])
def test_direct_compartment_management_rejects_noncanonical_code(db, code) -> None:
    manager = make_user(db, "compartment-admin", role="admin")

    with pytest.raises(ValueError, match="大寫識別碼"):
        create_security_compartment(
            db,
            actor=manager,
            code=code,
            name="Restricted project",
        )


def test_issue_collection_grant_and_revoke_are_audited(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    access = _collection_access(db, manager=manager, grant=grant, collection=collection)
    revoke_clearance_grant(db, actor=manager, clearance_grant_id=grant.id, now=NOW)

    events = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
    actions = [event.action for event in events]
    assert "clearance.grant_issued" in actions
    assert "clearance.collection_access_granted" in actions
    assert "clearance.grant_revoked" in actions
    for event in events:
        if event.action.startswith("clearance."):
            assert event.actor_user_id == manager.id
    assert access.clearance_grant_id == grant.id


def test_sqlite_check_constraints_reject_invalid_rows(db) -> None:
    manager = make_user(db, "manager", role="admin")
    subject = make_user(db, "subject")
    invalid = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level="公開",
        valid_from=NOW,
        expires_at=NOW + timedelta(hours=1),
        basis_ticket="SEC-BAD",
        issued_by_user_id=manager.id,
    )
    db.add(invalid)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    invalid_window = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level="無機密",
        valid_from=NOW,
        expires_at=NOW,
        basis_ticket="SEC-BAD-TIME",
        issued_by_user_id=manager.id,
    )
    db.add(invalid_window)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    invalid_revocation_pair = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level="無機密",
        valid_from=NOW,
        expires_at=NOW + timedelta(hours=1),
        basis_ticket="SEC-BAD-REVOKE",
        issued_by_user_id=manager.id,
        revoked_at=NOW,
        revoked_by_user_id=None,
    )
    db.add(invalid_revocation_pair)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    owner = make_user(db, "data-owner")
    collection = _collection(db, owner, level=Classification.UNCLASSIFIED)
    grant = _active_grant(
        db, manager=manager, subject=subject, level=Classification.UNCLASSIFIED
    )
    inert = CollectionAccessGrant(
        clearance_grant_id=grant.id,
        collection_id=collection.id,
        membership_granted=False,
        need_to_know=False,
        basis_ticket="NTK-BAD",
        issued_by_user_id=manager.id,
    )
    db.add(inert)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_clearance_management_api_is_admin_tier_only(client, db) -> None:
    make_user(db, "ordinary")
    make_user(db, "manager", role="admin")
    ordinary_headers = _auth_headers(client, "ordinary")
    denied = client.post(
        "/api/clearance/compartments",
        headers=ordinary_headers,
        json={"code": "API_A", "name": "API A"},
    )
    assert denied.status_code == 403

    manager_headers = _auth_headers(client, "manager")
    created = client.post(
        "/api/clearance/compartments",
        headers=manager_headers,
        json={"code": "API_A", "name": "API A"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["code"] == "API_A"


def test_migration_declares_r1_0013_constraints_and_predecessor() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "r1_0013_gate2_clearance_foundation.py"
    )
    text = path.read_text(encoding="utf-8")
    assert 'revision: str = "r1_0013"' in text
    assert 'down_revision: Union[str, None] = "r1_0012"' in text
    for table in (
        "security_compartments",
        "clearance_grants",
        "clearance_grant_compartments",
        "collection_access_grants",
        "collection_required_compartments",
        "document_required_compartments",
    ):
        assert f'"{table}"' in text
    for constraint in (
        "ck_clearance_grants_classification_level",
        "ck_clearance_grants_time_order",
        "ck_clearance_grants_revocation_pair",
        "uq_collection_access_grant",
        "ck_collection_access_grants_nonempty_authority",
    ):
        assert constraint in text
