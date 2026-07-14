# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance model, evaluator, audit, and management API tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from anila_contracts import Classification
from app.models.audit_log import AuditLog
from app.models.clearance import (
    ClearanceGrant,
    CollectionAccessGrant,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
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
    grant = _active_grant(
        db, manager=manager, subject=subject, level=grant_level
    )
    _collection_access(
        db, manager=manager, grant=grant, collection=collection
    )

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
    _collection_access(
        db, manager=manager, grant=grant, collection=collection
    )

    decision = resolve_and_evaluate_data_access(
        db, user_id=subject.id, collection_id=collection.id, now=NOW
    )
    assert decision.allowed is False


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_platform_role_without_clearance_never_bypasses_data_policy(db, role: str) -> None:
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
    _collection_access(
        db, manager=manager, grant=grant, collection=collection
    )
    if state == "revoked":
        revoke_clearance_grant(
            db, actor=manager, clearance_grant_id=grant.id, now=NOW
        )

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
    _collection_access(
        db, manager=manager, grant=access_grant, collection=collection
    )

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
    _collection_access(
        db, manager=manager, grant=grant, collection=collection
    )

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
    assert resolve_and_evaluate_data_access(
        db,
        user_id=subject.id,
        collection_id=collection.id,
        document_id=document.id,
        now=NOW,
    ).allowed is False

    add_grant_compartment(
        db,
        actor=manager,
        clearance_grant_id=grant.id,
        compartment_id=document_compartment.id,
    )
    assert resolve_and_evaluate_data_access(
        db,
        user_id=subject.id,
        collection_id=collection.id,
        document_id=document.id,
        now=NOW,
    ).allowed is True


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
    _collection_access(
        db, manager=manager, grant=grant, collection=collection
    )
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
    access = _collection_access(
        db, manager=manager, grant=grant, collection=collection
    )
    revoke_clearance_grant(
        db, actor=manager, clearance_grant_id=grant.id, now=NOW
    )

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
