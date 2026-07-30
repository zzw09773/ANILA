# -*- coding: utf-8 -*-
"""G9 — agent default classification level at register/update.

Covers:
- register with each of the four levels stores ``default_classification_level``
- unknown level refused with the same 422 shape as other validation failures
- derived ``requires_encryption`` matches the conversation mirror threshold
  (``level >= 密`` / RESTRICTED)
- conversation answered by a controlled-level agent latches via existing
  ``_agent_policy_level`` / ``_latch_agent_classification``, and outbound
  rules then behave accordingly
- level change writes an audit record naming old and new *effective* values
  (same transaction, result checked); stored transition kept as metadata
- administrator re-saving a legacy row at the same stored value still audits
  the effective drop when the derived boolean clears
- developer owner may raise but not lower the effective policy level
  (including the legacy boolean floor); administrator-tier may lower
- boolean-only repair commits when re-saving the same stored level with
  unchanged effective level (no classification audit)
- genuine no-op writes nothing
- illegal stored level refuses with 422 (not 500)
- authorization matches the existing agent-update rule (owner or admin-tier)
- no user-facing string in the dedicated endpoint claims encryption
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.api.proxy import _agent_policy_level, _latch_agent_classification
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.schemas.contracts.classification import (
    ClassificationLevel,
    outbound_action_allowed,
)
from tests.conftest import login, make_agent, make_model, make_user

pytestmark = pytest.mark.filterwarnings("ignore")

LEVELS = ["無機密", "營業秘密", "密", "機密"]
# Mirror threshold: classified / requires_encryption iff level >= 密.
DERIVED_BOOLEAN = {
    "無機密": False,
    "營業秘密": False,
    "密": True,
    "機密": True,
}


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, token, *, name: str, level: str, model_id: int):
    return client.post(
        "/api/agents/register",
        headers=_bearer(token),
        json={
            "name": name,
            "endpoint_url": "http://agent:9100",
            "description_for_router": "G9 classification level registration test agent",
            "base_model_id": model_id,
            "default_classification_level": level,
        },
    )


# ── Register stores each of the four levels ───────────────────────────────────


class TestRegisterStoresLevel:
    @pytest.mark.parametrize("level", LEVELS)
    def test_register_with_each_level(self, client, db, level):
        make_user(db, username=f"g9_reg_{level}", role="developer")
        model = make_model(db, name=f"g9-model-{level}")
        token = login(client, f"g9_reg_{level}")
        resp = _register(
            client, token, name=f"g9-agent-{level}", level=level, model_id=model.id
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["default_classification_level"] == level
        assert data["requires_encryption"] is DERIVED_BOOLEAN[level]

        from app.models.agent import Agent

        row = db.query(Agent).filter(Agent.name == f"g9-agent-{level}").one()
        assert row.default_classification_level == level
        assert row.requires_encryption is DERIVED_BOOLEAN[level]

    def test_unknown_level_refused_422(self, client, db):
        make_user(db, username="g9_bad", role="developer")
        model = make_model(db, name="g9-bad-model")
        token = login(client, "g9_bad")
        resp = _register(
            client, token, name="g9-bad-agent", level="極機密", model_id=model.id
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert "detail" in body


# ── Derived boolean matches mirror threshold ──────────────────────────────────


class TestDerivedBoolean:
    def test_requires_controlled_access_truth_table(self):
        from app.api.agents._common import requires_controlled_access

        for value, expected in DERIVED_BOOLEAN.items():
            level = ClassificationLevel.from_storage(value)
            assert requires_controlled_access(level) is expected, value

    def test_effective_matches_dispatch_legacy_floor(self, db):
        from app.api.agents._common import effective_agent_policy_level

        owner = make_user(db, username="g9_eff", role="developer")
        agent = make_agent(db, owner, name="g9-eff-legacy")
        agent.default_classification_level = "無機密"
        agent.requires_encryption = True
        db.commit()
        db.refresh(agent)
        assert effective_agent_policy_level(agent) is ClassificationLevel.RESTRICTED
        assert _agent_policy_level(agent) is ClassificationLevel.RESTRICTED

    @pytest.mark.parametrize("level", LEVELS)
    def test_classification_endpoint_derives_boolean(self, client, db, level):
        dev = make_user(db, username=f"g9_der_{level}", role="developer")
        agent = make_agent(db, dev, name=f"g9-der-{level}")
        token = login(client, f"g9_der_{level}")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": level},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["default_classification_level"] == level
        assert data["requires_encryption"] is DERIVED_BOOLEAN[level]
        db.refresh(agent)
        assert agent.default_classification_level == level
        assert agent.requires_encryption is DERIVED_BOOLEAN[level]


# ── Latch + outbound rules ────────────────────────────────────────────────────


class TestLatchAndOutbound:
    @pytest.mark.parametrize("level", LEVELS)
    def test_agent_policy_latch_and_outbound(self, client, db, level):
        user = make_user(db, username=f"g9_latch_{level}")
        owner = make_user(db, username=f"g9_latch_own_{level}", role="developer")
        agent = make_agent(db, owner, name=f"g9-latch-{level}")
        from app.api.agents._common import apply_default_classification_level

        apply_default_classification_level(
            agent, ClassificationLevel.from_storage(level)
        )
        db.commit()
        db.refresh(agent)

        policy = _agent_policy_level(agent)
        assert policy is ClassificationLevel.from_storage(level)

        conv = Conversation(user_id=user.id, title=f"g9-latch-{level}")
        db.add(conv)
        db.commit()
        db.refresh(conv)

        _latch_agent_classification(db, conv.id, policy.to_storage())
        db.commit()
        db.refresh(conv)
        assert conv.classification_level == level

        allow = outbound_action_allowed(ClassificationLevel.from_storage(level))
        if level in ("無機密", "營業秘密"):
            assert allow is True
        else:
            assert allow is False


# ── No-downgrade + legacy floor ───────────────────────────────────────────────


class TestNoDowngrade:
    def test_developer_may_raise_but_not_lower(self, client, db):
        dev = make_user(db, username="g9_nodown", role="developer")
        agent = make_agent(db, dev, name="g9-nodown-agent")
        token = login(client, "g9_nodown")
        up = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "機密"},
        )
        assert up.status_code == 200, up.text
        down = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "無機密"},
        )
        assert down.status_code == 403, down.text
        assert "不得降低" in down.json()["detail"]
        db.refresh(agent)
        assert agent.default_classification_level == "機密"
        assert agent.requires_encryption is True

    def test_admin_may_lower(self, client, db):
        dev = make_user(db, username="g9_adm_own", role="developer")
        make_user(db, username="g9_adm_low", role="admin")
        agent = make_agent(db, dev, name="g9-adm-low-agent")
        from app.api.agents._common import apply_default_classification_level

        apply_default_classification_level(agent, ClassificationLevel.SECRET)
        db.commit()
        token = login(client, "g9_adm_low")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.default_classification_level == "營業秘密"
        assert agent.requires_encryption is False

    def test_legacy_floor_blocks_developer_effective_downgrade(self, client, db):
        """Stored 無機密 + requires_encryption=true → effective 密.

        Setting stored level to 營業秘密 would clear the boolean and drop
        effective protection from 密 to 營業秘密 — developers must be refused.
        """
        dev = make_user(db, username="g9_legacy", role="developer")
        agent = make_agent(db, dev, name="g9-legacy-agent")
        agent.default_classification_level = "無機密"
        agent.requires_encryption = True
        db.commit()
        db.refresh(agent)
        assert _agent_policy_level(agent) is ClassificationLevel.RESTRICTED

        token = login(client, "g9_legacy")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 403, resp.text
        db.refresh(agent)
        assert agent.default_classification_level == "無機密"
        assert agent.requires_encryption is True
        assert _agent_policy_level(agent) is ClassificationLevel.RESTRICTED

    def test_admin_lower_clears_legacy_floor(self, client, db):
        dev = make_user(db, username="g9_leg_adm_dev", role="developer")
        make_user(db, username="g9_leg_adm", role="admin")
        agent = make_agent(db, dev, name="g9-leg-adm-agent")
        agent.default_classification_level = "無機密"
        agent.requires_encryption = True
        db.commit()
        token = login(client, "g9_leg_adm")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.default_classification_level == "營業秘密"
        assert agent.requires_encryption is False
        assert _agent_policy_level(agent) is ClassificationLevel.TRADE_SECRET

    def test_boolean_repair_same_level_commits(self, client, db):
        """Re-saving the same stored level repairs a stale derived boolean.

        Effective level is already 密 (stored), so no set_classification
        audit — only the boolean is fixed.
        """
        make_user(db, username="g9_repair_adm", role="admin")
        owner = make_user(db, username="g9_repair_own", role="developer")
        agent = make_agent(db, owner, name="g9-repair-agent")
        agent.default_classification_level = "密"
        agent.requires_encryption = False  # should be True
        db.commit()
        token = login(client, "g9_repair_adm")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "密"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["requires_encryption"] is True
        db.refresh(agent)
        assert agent.default_classification_level == "密"
        assert agent.requires_encryption is True
        # Effective unchanged → no set_classification audit row.
        rows = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_id == str(agent.id),
            )
            .all()
        )
        assert rows == []

    def test_admin_legacy_same_stored_audits_effective_drop(self, client, db):
        """Admin re-saves legacy row at same stored value → effective drop audited.

        Stored 無機密 + requires_encryption=true → effective 密. Re-saving
        無機密 clears the boolean; outbound goes from blocked to allowed.
        Audit must name 密 → 無機密 even though the stored column is unchanged.
        """
        make_user(db, username="g9_leg_resave_adm", role="admin")
        owner = make_user(db, username="g9_leg_resave_own", role="developer")
        agent = make_agent(db, owner, name="g9-leg-resave-agent")
        agent.default_classification_level = "無機密"
        agent.requires_encryption = True
        db.commit()
        db.refresh(agent)
        assert _agent_policy_level(agent) is ClassificationLevel.RESTRICTED

        token = login(client, "g9_leg_resave_adm")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "無機密"},
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.default_classification_level == "無機密"
        assert agent.requires_encryption is False
        assert _agent_policy_level(agent) is ClassificationLevel.UNCLASSIFIED

        row = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_type == "agent",
                AuditLog.resource_id == str(agent.id),
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert "密" in (row.detail or "")
        assert "無機密" in (row.detail or "")
        assert "密 → 無機密" in (row.detail or "")
        meta = json.loads(row.metadata_json) if row.metadata_json else {}
        assert meta.get("from_level") == "密"
        assert meta.get("to_level") == "無機密"
        assert meta.get("from_stored_level") == "無機密"
        assert meta.get("to_stored_level") == "無機密"
        assert meta.get("requires_controlled_access") is False

    def test_admin_legacy_genuine_downgrade_audits_effective(self, client, db):
        """Genuine downgrade on legacy row records effective, not stored, transition.

        Stored 無機密 + bool → effective 密; admin sets 營業秘密.
        Audit must say 密 → 營業秘密 (not 無機密 → 營業秘密).
        """
        make_user(db, username="g9_leg_down_adm", role="admin")
        owner = make_user(db, username="g9_leg_down_own", role="developer")
        agent = make_agent(db, owner, name="g9-leg-down-agent")
        agent.default_classification_level = "無機密"
        agent.requires_encryption = True
        db.commit()

        token = login(client, "g9_leg_down_adm")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.default_classification_level == "營業秘密"
        assert agent.requires_encryption is False

        row = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_id == str(agent.id),
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert "密 → 營業秘密" in (row.detail or "")
        meta = json.loads(row.metadata_json) if row.metadata_json else {}
        assert meta.get("from_level") == "密"
        assert meta.get("to_level") == "營業秘密"
        assert meta.get("from_stored_level") == "無機密"
        assert meta.get("to_stored_level") == "營業秘密"

    def test_noop_writes_no_audit(self, client, db):
        """Re-saving an already-consistent row writes nothing."""
        make_user(db, username="g9_noop_adm", role="admin")
        owner = make_user(db, username="g9_noop_own", role="developer")
        agent = make_agent(db, owner, name="g9-noop-agent")
        from app.api.agents._common import apply_default_classification_level

        apply_default_classification_level(
            agent, ClassificationLevel.TRADE_SECRET
        )
        db.commit()
        db.refresh(agent)

        token = login(client, "g9_noop_adm")
        before_class = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_type == "agent",
                AuditLog.resource_id == str(agent.id),
            )
            .count()
        )
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 200, resp.text
        assert "未變更" in resp.json().get("message", "")
        db.refresh(agent)
        assert agent.default_classification_level == "營業秘密"
        assert agent.requires_encryption is False
        after_class = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_type == "agent",
                AuditLog.resource_id == str(agent.id),
            )
            .count()
        )
        assert after_class == before_class == 0

    def test_illegal_stored_level_refused_422(self, client, db):
        dev = make_user(db, username="g9_ill", role="developer")
        agent = make_agent(db, dev, name="g9-ill-agent")
        agent.default_classification_level = "極機密"
        db.commit()
        token = login(client, "g9_ill")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "密"},
        )
        assert resp.status_code == 422, resp.text
        assert "無效" in resp.json()["detail"]


# ── Audit + authorization ─────────────────────────────────────────────────────


class TestAuditAndAuth:
    def test_level_change_writes_audit_with_from_to(self, client, db):
        dev = make_user(db, username="g9_audit", role="developer")
        agent = make_agent(db, dev, name="g9-audit-agent")
        assert agent.default_classification_level == "無機密"
        token = login(client, "g9_audit")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 200, resp.text

        row = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_type == "agent",
                AuditLog.resource_id == str(agent.id),
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.actor_user_id == dev.id
        assert "無機密" in (row.detail or "")
        assert "營業秘密" in (row.detail or "")
        assert "無機密 → 營業秘密" in (row.detail or "")
        meta = json.loads(row.metadata_json) if row.metadata_json else {}
        assert meta.get("from_level") == "無機密"
        assert meta.get("to_level") == "營業秘密"
        assert meta.get("from_stored_level") == "無機密"
        assert meta.get("to_stored_level") == "營業秘密"
        assert meta.get("requires_controlled_access") is False

    def test_update_route_legacy_same_stored_audits_effective_drop(self, client, db):
        """PUT /api/agents/{id} same path: effective drop audited on legacy resave."""
        make_user(db, username="g9_upd_leg_adm", role="admin")
        owner = make_user(db, username="g9_upd_leg_own", role="developer")
        model = make_model(db, name="g9-upd-leg-model")
        agent = make_agent(db, owner, name="g9-upd-leg-agent")
        agent.base_model_id = model.id
        agent.default_classification_level = "無機密"
        agent.requires_encryption = True
        db.commit()

        token = login(client, "g9_upd_leg_adm")
        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_bearer(token),
            json={"default_classification_level": "無機密"},
        )
        assert resp.status_code == 200, resp.text
        db.refresh(agent)
        assert agent.requires_encryption is False

        row = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "set_classification",
                AuditLog.resource_id == str(agent.id),
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert "密 → 無機密" in (row.detail or "")
        meta = json.loads(row.metadata_json) if row.metadata_json else {}
        assert meta.get("from_level") == "密"
        assert meta.get("to_level") == "無機密"

    def test_audit_checked_before_commit(self, client, db, monkeypatch):
        """Failed audit aborts before commit — change must not land."""
        from app.api.agents import credentials as cred_mod

        dev = make_user(db, username="g9_audfail", role="developer")
        agent = make_agent(db, dev, name="g9-audfail-agent")
        token = login(client, "g9_audfail")

        def _fail_audit(*_args, **_kwargs):
            return None

        monkeypatch.setattr(cred_mod, "log_audit_event", _fail_audit)
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "密"},
        )
        assert resp.status_code == 500, resp.text
        assert "稽核" in resp.json()["detail"]
        db.refresh(agent)
        assert agent.default_classification_level == "無機密"
        assert agent.requires_encryption is False

    def test_owner_can_set_level(self, client, db):
        dev = make_user(db, username="g9_owner", role="developer")
        agent = make_agent(db, dev, name="g9-owner-agent")
        token = login(client, "g9_owner")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "密"},
        )
        assert resp.status_code == 200, resp.text

    def test_admin_can_set_level(self, client, db):
        dev = make_user(db, username="g9_adm_dev", role="developer")
        make_user(db, username="g9_adm", role="admin")
        agent = make_agent(db, dev, name="g9-adm-agent")
        token = login(client, "g9_adm")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "機密"},
        )
        assert resp.status_code == 200, resp.text

    def test_non_owner_developer_forbidden(self, client, db):
        owner = make_user(db, username="g9_own2", role="developer")
        make_user(db, username="g9_other", role="developer")
        agent = make_agent(db, owner, name="g9-forbid-agent")
        token = login(client, "g9_other")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "密"},
        )
        assert resp.status_code == 403, resp.text

    def test_update_agent_accepts_level(self, client, db):
        dev = make_user(db, username="g9_upd", role="developer")
        model = make_model(db, name="g9-upd-model")
        agent = make_agent(db, dev, name="g9-upd-agent")
        agent.base_model_id = model.id
        db.commit()
        token = login(client, "g9_upd")
        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_bearer(token),
            json={"default_classification_level": "營業秘密"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["default_classification_level"] == "營業秘密"
        assert resp.json()["requires_encryption"] is False
        db.refresh(agent)
        assert agent.default_classification_level == "營業秘密"

    def test_update_agent_refuses_developer_downgrade(self, client, db):
        dev = make_user(db, username="g9_upd_down", role="developer")
        model = make_model(db, name="g9-upd-down-model")
        agent = make_agent(db, dev, name="g9-upd-down-agent")
        agent.base_model_id = model.id
        from app.api.agents._common import apply_default_classification_level

        apply_default_classification_level(agent, ClassificationLevel.SECRET)
        db.commit()
        token = login(client, "g9_upd_down")
        resp = client.put(
            f"/api/agents/{agent.id}",
            headers=_bearer(token),
            json={"default_classification_level": "無機密"},
        )
        assert resp.status_code == 403, resp.text
        db.refresh(agent)
        assert agent.default_classification_level == "機密"


# ── Wording: no encryption claims on the new endpoint surface ─────────────────


class TestNoEncryptionWording:
    def test_classification_response_message_has_no_encryption_claim(self, client, db):
        dev = make_user(db, username="g9_word", role="developer")
        agent = make_agent(db, dev, name="g9-word-agent")
        token = login(client, "g9_word")
        resp = client.post(
            f"/api/agents/{agent.id}/classification",
            headers=_bearer(token),
            json={"default_classification_level": "密"},
        )
        assert resp.status_code == 200, resp.text
        message = resp.json().get("message", "")
        lowered = message.lower()
        assert "加密" not in message
        assert "encryption" not in lowered
        assert "encrypted" not in lowered
        assert "分類等級" in message
