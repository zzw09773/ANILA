"""Gate 2 G3-G5 memory classification and clearance regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker

from anila_contracts import Classification
from app.api.proxy import _latch_inherited_classification
from app.models.audit_log import AuditLog
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    SecurityCompartment,
)
from app.models.conversation import Conversation
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from app.models.task import Task
from app.models.user import User
from app.models.user_memory import (
    UserFact,
    UserFactRequiredCompartment,
    UserFactSourceCollection,
)
from app.middleware.caller import Caller
from app.services import memory_service
from app.services.memory_service import MemoryPolicyDataError


def _user(db, username: str, *, role: str = "user") -> User:
    row = User(
        username=username,
        email=f"{username}@example.invalid",
        hashed_password="test-only",
        role=role,
        is_active=True,
        is_approved=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _conversation(
    db, user: User, *, level: Classification, collection_id: int | None = None
) -> Conversation:
    row = Conversation(
        user_id=user.id,
        title="memory-governance",
        collection_id=collection_id,
        classification_level=level.to_storage(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _collection(
    db, owner: User, *, level: Classification
) -> IngestionCollection:
    row = IngestionCollection(
        name="classified-memory-source",
        chunking_config={},
        embedding_model="test",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4000,
        created_by=owner.id,
        classification_level=level.to_storage(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _compartment(db, issuer: User, code: str) -> SecurityCompartment:
    row = SecurityCompartment(
        code=code,
        name=code,
        created_by_user_id=issuer.id,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _grant(
    db,
    *,
    issuer: User,
    subject: User,
    level: Classification,
    compartment_ids: set[int] | None = None,
    collection: IngestionCollection | None = None,
    membership: bool = True,
    need_to_know: bool = True,
) -> ClearanceGrant:
    now = datetime.now(timezone.utc)
    row = ClearanceGrant(
        subject_user_id=subject.id,
        max_classification_level=level.to_storage(),
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        basis_ticket="TEST-GATE2",
        issued_by_user_id=issuer.id,
    )
    db.add(row)
    db.flush()
    for compartment_id in compartment_ids or set():
        db.add(
            ClearanceGrantCompartment(
                clearance_grant_id=row.id,
                compartment_id=compartment_id,
            )
        )
    if collection is not None:
        db.add(
            CollectionAccessGrant(
                clearance_grant_id=row.id,
                collection_id=collection.id,
                membership_granted=membership,
                need_to_know=need_to_know,
                basis_ticket="TEST-GATE2",
                issued_by_user_id=issuer.id,
            )
        )
    db.commit()
    db.refresh(row)
    return row


def _fact(
    db,
    *,
    user: User,
    conversation: Conversation,
    level: Classification,
    compartment_ids: set[int] | None = None,
    collection_ids: set[int] | None = None,
) -> UserFact:
    row = UserFact(
        user_id=user.id,
        key="classified.preference",
        value="only for cleared callers",
        source_conversation_id=conversation.id,
        classification_level=level.to_storage(),
        classification_source="test:source_conversation",
    )
    db.add(row)
    db.flush()
    for compartment_id in compartment_ids or set():
        db.add(
            UserFactRequiredCompartment(
                fact_id=row.id, compartment_id=compartment_id
            )
        )
    for collection_id in collection_ids or set():
        db.add(
            UserFactSourceCollection(fact_id=row.id, collection_id=collection_id)
        )
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_top_secret_fact_requires_one_grant_covering_level_compartment_and_ntk(
    db, monkeypatch
):
    issuer = _user(db, "memory-issuer", role="owner")
    subject = _user(db, "memory-subject", role="owner")
    collection = _collection(db, issuer, level=Classification.TOP_SECRET)
    compartment = _compartment(db, issuer, "PROGRAM_ALPHA")
    other_compartment = _compartment(db, issuer, "PROGRAM_ALPHA_OTHER")
    db.add(
        CollectionRequiredCompartment(
            collection_id=collection.id,
            compartment_id=compartment.id,
            basis_ticket="TEST-GATE2",
            assigned_by_user_id=issuer.id,
        )
    )
    db.commit()
    conversation = _conversation(
        db,
        subject,
        level=Classification.TOP_SECRET,
        collection_id=collection.id,
    )
    _fact(
        db,
        user=subject,
        conversation=conversation,
        level=Classification.TOP_SECRET,
        compartment_ids={compartment.id},
        collection_ids={collection.id},
    )
    grant = _grant(
        db,
        issuer=issuer,
        subject=subject,
        level=Classification.TOP_SECRET,
        compartment_ids={compartment.id},
        collection=collection,
    )

    facts = memory_service.get_user_facts(db, subject.id)
    assert [fact.value for fact in facts] == ["only for cleared callers"]
    assert facts[0].classification_level is Classification.TOP_SECRET
    monkeypatch.setattr(memory_service.settings, "ENABLE_MEMORY", True)
    recalled = await memory_service.build_memory_block(
        db,
        subject.id,
        "recall my governed fact",
        exclude_conversation_id=conversation.id,
    )
    assert "only for cleared callers" in (recalled.block or "")
    assert recalled.inherited_classification is Classification.TOP_SECRET

    # Same level but a different compartment is a horizontal denial.
    membership = db.get(
        ClearanceGrantCompartment,
        {
            "clearance_grant_id": grant.id,
            "compartment_id": compartment.id,
        },
    )
    assert membership is not None
    db.delete(membership)
    db.add(
        ClearanceGrantCompartment(
            clearance_grant_id=grant.id,
            compartment_id=other_compartment.id,
        )
    )
    db.commit()
    assert memory_service.get_user_facts(db, subject.id) == []
    denied = await memory_service.build_memory_block(
        db,
        subject.id,
        "recall my governed fact",
        exclude_conversation_id=conversation.id,
    )
    assert denied.block is None


@pytest.mark.asyncio
async def test_grant_expiry_during_embedding_is_rechecked_before_injection(
    monkeypatch,
):
    grant = memory_service._ActiveMemoryGrant(
        grant_id=1,
        max_level=Classification.TOP_SECRET,
        compartment_ids=frozenset(),
        collection_access={},
    )
    fact = memory_service.UserFactDTO(
        id=1,
        user_id=1,
        key="high.fact",
        value="must not inject after expiry",
        classification_level=Classification.TOP_SECRET,
        classification_source="test",
    )
    grant_reads = iter(((grant,), ()))

    def load_grants(*args, **kwargs):
        return next(grant_reads)

    def facts_for_current_grants(*args, grants=None, **kwargs):
        return [fact] if grants else []

    async def embedding_window(*args, **kwargs):
        return []

    monkeypatch.setattr(memory_service.settings, "ENABLE_MEMORY", True)
    monkeypatch.setattr(memory_service, "_load_active_memory_grants", load_grants)
    monkeypatch.setattr(memory_service, "get_user_facts", facts_for_current_grants)
    monkeypatch.setattr(
        memory_service, "retrieve_relevant_chunks", embedding_window
    )

    result = await memory_service.build_memory_block(
        object(),
        1,
        "query",
        exclude_conversation_id=2,
    )
    assert result.block is None
    assert result.inherited_classification is None


def test_owner_role_and_partial_cross_grant_authority_never_bypass_clearance(db):
    issuer = _user(db, "memory-admin", role="owner")
    subject = _user(db, "memory-role-owner", role="owner")
    collection = _collection(db, issuer, level=Classification.SECRET)
    compartment = _compartment(db, issuer, "PROGRAM_BETA")
    conversation = _conversation(
        db, subject, level=Classification.SECRET, collection_id=collection.id
    )
    _fact(
        db,
        user=subject,
        conversation=conversation,
        level=Classification.SECRET,
        compartment_ids={compartment.id},
        collection_ids={collection.id},
    )

    # Role alone has no data authority.
    assert memory_service.get_user_facts(db, subject.id) == []

    # One grant has level/compartment but no NTK; another has NTK but too-low
    # level.  The evaluator must not compose them.
    _grant(
        db,
        issuer=issuer,
        subject=subject,
        level=Classification.SECRET,
        compartment_ids={compartment.id},
    )
    _grant(
        db,
        issuer=issuer,
        subject=subject,
        level=Classification.UNCLASSIFIED,
        collection=collection,
    )
    assert memory_service.get_user_facts(db, subject.id) == []


def test_unknown_or_null_memory_classification_fails_closed(db):
    with pytest.raises(MemoryPolicyDataError, match="NULL"):
        memory_service._effective_memory_requirement(
            db,
            user_id=1,
            stored_level=None,
            classification_source="test",
            required_compartment_ids=frozenset(),
            source_collection_ids=frozenset(),
            source_conversation_id=None,
            source_task_id=None,
            source_snapshot_id=None,
        )
    with pytest.raises(MemoryPolicyDataError, match="未知"):
        memory_service._effective_memory_requirement(
            db,
            user_id=1,
            stored_level="COSMIC",
            classification_source="test",
            required_compartment_ids=frozenset(),
            source_collection_ids=frozenset(),
            source_conversation_id=None,
            source_task_id=None,
            source_snapshot_id=None,
        )


def test_write_context_inherits_highest_task_source_and_compartment(db):
    issuer = _user(db, "memory-write-issuer", role="owner")
    subject = _user(db, "memory-write-subject")
    collection = _collection(db, issuer, level=Classification.SECRET)
    compartment = _compartment(db, issuer, "PROGRAM_GAMMA")
    db.add(
        CollectionRequiredCompartment(
            collection_id=collection.id,
            compartment_id=compartment.id,
            basis_ticket="TEST-GATE2",
            assigned_by_user_id=issuer.id,
        )
    )
    conversation = _conversation(
        db,
        subject,
        level=Classification.TRADE_SECRET,
        collection_id=collection.id,
    )
    task = Task(
        title="classified memory turn",
        task_type="query",
        requester_user_id=subject.id,
        conversation_id=conversation.id,
        source_scope="collection",
        selected_collection_ids=[collection.id],
        classification_level=Classification.TOP_SECRET.to_storage(),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    _user_row, context = memory_service._resolve_write_context(
        db,
        user_id=subject.id,
        conversation_id=conversation.id,
        task_id=task.id,
        input_classification=Classification.CONFIDENTIAL,
        inherited_compartment_ids=frozenset(),
        inherited_source_collection_ids=frozenset(),
        is_encrypted=False,
    )

    assert context.classification_level is Classification.TOP_SECRET
    assert context.required_compartment_ids == frozenset({compartment.id})
    assert context.source_collection_ids == frozenset({collection.id})
    db.refresh(conversation)
    assert conversation.classification_level == Classification.TOP_SECRET.to_storage()


def test_latch_uses_actual_highest_memory_level_not_confidential_floor(db):
    user = _user(db, "memory-latch-user")
    conversation = _conversation(db, user, level=Classification.UNCLASSIFIED)

    _latch_inherited_classification(db, conversation.id, Classification.TOP_SECRET)

    db.refresh(conversation)
    assert conversation.classification_level == Classification.TOP_SECRET.to_storage()
    assert conversation.classification_inherited is True


@pytest.mark.asyncio
async def test_latch_failure_allows_read_only_resolution_but_not_dispatch(
    db, monkeypatch
):
    from app.api import proxy

    user = _user(db, "memory-latch-failure-user")
    conversation = _conversation(db, user, level=Classification.UNCLASSIFIED)
    model = ModelRegistry(
        name="memory-latch-target",
        display_name="memory-latch-target",
        model_type="llm",
        endpoint_url="http://must-not-dispatch.invalid",
        is_active=True,
        is_router_primary=True,
        classification_ceiling=Classification.TOP_SECRET.to_storage(),
    )
    db.add(model)
    db.commit()

    class _Request:
        headers = {"X-ANILA-Conversation-Id": str(conversation.id)}

        async def json(self):
            return {
                "model": model.name,
                "messages": [{"role": "user", "content": "classified recall"}],
            }

    async def recalled(*args, **kwargs):
        return memory_service.MemoryReadResult(
            block="classified memory",
            facts_count=1,
            inherited_classification=Classification.TOP_SECRET,
        )

    def latch_failure(*args, **kwargs):
        raise RuntimeError("synthetic latch failure")

    def must_not_reach_dispatch(*args, **kwargs):
        raise AssertionError("latch failure must remain zero outbound")

    monkeypatch.setattr(proxy, "_inject_memory", recalled)
    monkeypatch.setattr(proxy, "_latch_inherited_classification", latch_failure)
    monkeypatch.setattr(proxy, "proxy_request", must_not_reach_dispatch)

    with pytest.raises(HTTPException) as exc_info:
        await proxy.chat_completions(
            _Request(), caller=Caller(user=user, api_key_id=None), db=db
        )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_model_ceiling_denial_prevents_memory_gateway_dispatch_and_is_audited(
    db, monkeypatch
):
    user = _user(db, "memory-ceiling-user")
    # The persisted conversation is deliberately low.  The actual memory
    # input/task/source authority is TOP SECRET and must be the ceiling input.
    conversation = _conversation(db, user, level=Classification.UNCLASSIFIED)
    model = ModelRegistry(
        name="memory-low-ceiling",
        display_name="memory-low-ceiling",
        model_type="embedding",
        endpoint_url="http://never-called.invalid",
        is_active=True,
        classification_ceiling=Classification.SECRET.to_storage(),
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(memory_service, "SessionLocal", factory)
    dispatched = False

    async def must_not_dispatch(*args, **kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("ceiling deny must happen before gateway dispatch")

    monkeypatch.setattr(memory_service, "proxy_request", must_not_dispatch)

    with pytest.raises(HTTPException) as exc_info:
        await memory_service._gateway_request(
            db,
            model=model,
            user=user,
            conversation_id=conversation.id,
            classification_level=Classification.TOP_SECRET,
            request_body={"model": model.name, "input": ["classified"]},
            endpoint_path="/v1/embeddings",
            purpose="embedding",
            task_id=None,
            trace_id=None,
        )
    assert exc_info.value.status_code == 403
    assert dispatched is False
    db.expire_all()
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "memory.model_inference")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert audit.status == "denied"


def test_clearance_is_loaded_fresh_per_user_and_after_session_restart(db):
    issuer = _user(db, "memory-pool-issuer", role="owner")
    first = _user(db, "memory-pool-first")
    second = _user(db, "memory-pool-second")
    grant = _grant(
        db,
        issuer=issuer,
        subject=first,
        level=Classification.TOP_SECRET,
    )

    assert memory_service._load_active_memory_grants(db, user_id=first.id)
    assert memory_service._load_active_memory_grants(db, user_id=second.id) == ()

    issuer_id = int(issuer.id)
    first_id = int(first.id)
    grant.revoked_at = datetime.now(timezone.utc)
    grant.revoked_by_user_id = issuer_id
    db.commit()
    factory = sessionmaker(bind=db.get_bind())
    db.close()
    restarted = factory()
    try:
        assert memory_service._load_active_memory_grants(
            restarted, user_id=first_id
        ) == ()
    finally:
        restarted.close()
