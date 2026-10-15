"""Owner-only conversation retargeting, synthetic rows and no external calls."""
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import ValidationError
import pytest

from app.api import conversations as api
from app.models.agent import Agent, UserAgentPermission
from app.models.conversation import Conversation, ConversationShare, ConversationUserMeta
from app.models.department import Department
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import QuickAssistant, QuickVersion
from app.models.user import User
from app.services import conversation_service as svc


@pytest.fixture
def targets(db):
    unit, other_unit = Department(name="target-unit"), Department(name="target-other-unit")
    db.add_all([unit, other_unit])
    db.flush()
    users = [User(username=name, hashed_password="synthetic-not-a-password", role=role,
                  department_id=department.id, is_active=True, is_approved=True)
             for name, role, department in (("target-owner", "user", unit), ("target-peer", "user", unit),
                                           ("target-other", "user", other_unit), ("target-admin", "admin", unit))]
    owner, peer, other, admin = users
    model = ModelRegistry(name="target-model", display_name="合成目標模型", endpoint_url="http://model.invalid", model_type="llm",
                          is_active=True, classification_ceiling="機密")
    db.add_all([*users, model])
    db.flush()
    agents = [Agent(name=name, endpoint_url="http://agent.invalid", owner_user_id=admin.id,
                    base_model_id=model.id, approval_status="approved", classification_ceiling="機密",
                    default_classification_level=level)
              for name, level in (("target-first", "無機密"), ("target-second", "無機密"), ("target-high", "密"),
                                  ("target-hidden", "無機密"))]
    db.add_all(agents)
    db.flush()
    first, second, high, hidden = agents
    db.add_all([UserAgentPermission(user_id=owner.id, agent_id=agent.id) for agent in (first, second, high)])
    db.commit()
    conv = svc.create_conversation(db, owner.id, title="原始標題", agent_id=first.id)
    return SimpleNamespace(owner=owner, peer=peer, other=other, admin=admin, model=model,
                           first=first, second=second, high=high, hidden=hidden, conv=conv,
                           unit=unit, other_unit=other_unit)


def _patch(db, rows, payload, *, user=None, expected_user_id=None):
    return api.update_conversation(rows.conv.id, api.ConversationUpdate(**payload), db,
                                   user or rows.owner, expected_user_id)


def test_partial_target_schema_distinguishes_omit_clear_and_positive_integer():
    assert "agent_id" not in api.ConversationUpdate(starred=True).model_fields_set
    assert "agent_id" in api.ConversationUpdate(agent_id=None).model_fields_set
    assert api.ConversationUpdate(agent_id=3).agent_id == 3
    for invalid in (0, -1, True, "3", 1.5, "quick-name"):
        with pytest.raises(ValidationError):
            api.ConversationUpdate(agent_id=invalid)


def test_target_switch_omission_and_clear_persist_without_losing_metadata(db, targets):
    changed = _patch(db, targets, {"agent_id": targets.second.id, "title": "新標題", "starred": True})
    assert changed.agent_id == targets.second.id and changed.title == "新標題"
    db.expire_all()
    assert db.get(Conversation, targets.conv.id).agent_id == targets.second.id
    assert _patch(db, targets, {"folder": "saved"}).agent_id == targets.second.id
    cleared = _patch(db, targets, {"agent_id": None})
    assert cleared.agent_id is None and cleared.starred and cleared.folder == "saved"
    db.expire_all()
    assert db.get(Conversation, targets.conv.id).agent_id is None


@pytest.mark.parametrize("actor", ["peer", "other", "admin"])
@pytest.mark.parametrize("target", ["second", None])
def test_only_owner_can_switch_or_clear_even_when_shared_or_admin(db, targets, actor, target):
    db.add(ConversationShare(conversation_id=targets.conv.id, target_user_id=targets.peer.id))
    db.commit()
    with pytest.raises(HTTPException) as caught:
        _patch(db, targets, {"agent_id": getattr(targets, target).id if target else None}, user=getattr(targets, actor))
    assert caught.value.status_code == 404
    db.rollback()
    assert db.get(Conversation, targets.conv.id).agent_id == targets.first.id


@pytest.mark.parametrize("failure", ["missing", "hidden", "disabled", "registered", "unavailable", "offline_model"])
def test_unavailable_and_unauthorized_targets_share_one_error_and_do_not_write(db, targets, failure):
    target_id = targets.second.id
    if failure == "missing":
        target_id = 999999
    elif failure == "hidden":
        target_id = targets.hidden.id
    elif failure in ("disabled", "registered"):
        targets.second.approval_status = failure
    elif failure == "unavailable":
        targets.second.unavailable_reason = "base_model_offline"
    else:
        targets.model.is_active = False
    db.commit()
    with pytest.raises(HTTPException) as caught:
        _patch(db, targets, {"agent_id": target_id, "title": "不可留下", "starred": True})
    assert (caught.value.status_code, caught.value.detail) == (404, "找不到可使用的助手")
    db.rollback()
    stored = db.get(Conversation, targets.conv.id)
    assert stored.agent_id == targets.first.id and stored.title == "原始標題"
    assert db.query(ConversationUserMeta).filter_by(conversation_id=stored.id).count() == 0


@pytest.mark.parametrize("failure", ["disabled", "stale_version", "cross_unit"])
def test_quick_version_eligibility_is_checked_beyond_plain_agent_grants(db, targets, failure):
    assistant = QuickAssistant(owner_user_id=targets.admin.id,
                               department_id=targets.other_unit.id if failure == "cross_unit" else targets.unit.id,
                               draft_revision=1, published_revision=2 if failure == "stale_version" else 1,
                               disabled=failure == "disabled")
    db.add(assistant)
    db.flush()
    version = QuickVersion(assistant_id=assistant.id, revision=1, payload={"scope": "unit"}, payload_hash="a" * 64,
                           created_by=targets.admin.id, agent_id=targets.second.id, status="published")
    db.add(version)
    db.commit()
    with pytest.raises(HTTPException) as caught:
        _patch(db, targets, {"agent_id": targets.second.id})
    assert caught.value.status_code == 404
    db.rollback()
    assert db.get(Conversation, targets.conv.id).agent_id == targets.first.id


def test_switch_and_clear_latch_classification_without_downgrading(db, targets):
    elevated = _patch(db, targets, {"agent_id": targets.high.id})
    assert elevated.classification_level == "密" and elevated.classified
    lower_policy = _patch(db, targets, {"agent_id": targets.first.id})
    assert lower_policy.classification_level == "密" and lower_policy.classified
    cleared = _patch(db, targets, {"agent_id": None})
    assert cleared.classification_level == "密" and cleared.classified


@pytest.mark.parametrize("ceiling_target", ["agent", "model"])
def test_ceiling_refusal_does_not_downgrade_or_partially_update(db, targets, ceiling_target):
    _patch(db, targets, {"agent_id": targets.high.id})
    target = targets.second if ceiling_target == "agent" else targets.model
    target.classification_ceiling = "無機密"
    db.commit()
    with pytest.raises(HTTPException) as caught:
        _patch(db, targets, {"agent_id": targets.second.id, "title": "不可留下"})
    assert caught.value.status_code == 403
    db.rollback()
    stored = db.get(Conversation, targets.conv.id)
    assert stored.classification_level == "密" and stored.agent_id == targets.high.id
    assert stored.title == "原始標題"


def test_create_uses_same_authorization_and_policy_gate_atomically(db, targets):
    before = db.query(Conversation).count()
    with pytest.raises(HTTPException) as caught:
        svc.create_conversation(db, targets.owner.id, agent_id=targets.hidden.id)
    assert caught.value.status_code == 404
    assert db.query(Conversation).count() == before
    created = svc.create_conversation(db, targets.owner.id, agent_id=targets.high.id)
    assert created.classification_level == "密" and created.classified
    before = db.query(Conversation).count()
    with pytest.raises(HTTPException):
        svc.create_conversation(db, targets.owner.id, agent_id=targets.high.id, origin="anilalm")
    assert db.query(Conversation).count() == before


def test_identity_header_mismatch_rejects_before_target_lookup(db, targets, monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("identity guard must run before any target write")

    monkeypatch.setattr(svc, "update_conversation", unreachable)
    with pytest.raises(HTTPException) as caught:
        _patch(db, targets, {"agent_id": targets.second.id}, expected_user_id=targets.other.id)
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_http_put_roundtrip_and_identity_header(db, targets):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = lambda: db
    app.dependency_overrides[api.get_current_user] = lambda: targets.owner
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://synthetic.test") as client:
        response = await client.put(f"/api/conversations/{targets.conv.id}", json={"agent_id": targets.second.id},
                                     headers={"X-ANILA-Expected-User-ID": str(targets.owner.id)})
        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == targets.second.id
        reloaded = await client.get(f"/api/conversations/{targets.conv.id}")
        assert reloaded.status_code == 200 and reloaded.json()["agent_id"] == targets.second.id
        mismatch = await client.put(f"/api/conversations/{targets.conv.id}", json={"agent_id": None},
                                     headers={"X-ANILA-Expected-User-ID": str(targets.other.id)})
        assert mismatch.status_code == 409
        invalid = await client.put(f"/api/conversations/{targets.conv.id}", json={"agent_id": True})
        assert invalid.status_code == 422
