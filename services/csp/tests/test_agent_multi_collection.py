"""P4.7 — agent may bind zero / one / several knowledge bases.

Covers: multi-collection search scope (no silent first-only), unbound
behaviour, register/update set acceptance, legacy single-field
derivation, migration backfill of an existing single binding, audit
before→after sets, and authorization matching ``_require_agent_editor``.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api.ingestion.search import SearchPrincipal, _enforce_agent_collection_scope
from app.models.agent import Agent, AgentCollectionBinding
from app.models.audit_log import AuditLog
from app.models.ingestion import IngestionCollection
from app.services.agent_collection_bindings import (
    get_bound_collection_ids,
    resolve_requested_collection_ids,
    set_bound_collection_ids,
)
from tests.conftest import login, make_agent, make_model, make_user


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


# ── helpers ──────────────────────────────────────────────────────────────────


def _collection(db, owner, name: str) -> IngestionCollection:
    c = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model="nvidia/NV-embed-V2",
        embedding_dim=4000,
        created_by=owner.id,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _transfer_collection(db, collection: IngestionCollection, new_owner) -> None:
    """Make ``collection`` inaccessible to its previous ``created_by`` owner.

    Non-admin access is ``created_by == user.id``; transferring away is how
    a pre-existing binding becomes genuinely stale for the agent owner.
    """
    collection.created_by = new_owner.id
    db.commit()
    db.refresh(collection)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeAgent:
    """Mirrors the real Agent surface the search guard reads."""

    def __init__(self, *, bound_collection_ids=None, bound_collection_id=None):
        self.collection_bindings = [
            AgentCollectionBinding(collection_id=cid)
            for cid in (bound_collection_ids or [])
        ]
        self.bound_collection_id = bound_collection_id


# ── search-scope reader ──────────────────────────────────────────────────────


class TestSearchScopeMultiCollection:
    def test_agent_with_two_collections_may_search_both(self):
        agent = _FakeAgent(bound_collection_ids=[5, 9], bound_collection_id=5)
        principal = SearchPrincipal(user=object(), agent=agent)
        _enforce_agent_collection_scope(principal, collection_id=5)
        _enforce_agent_collection_scope(principal, collection_id=9)

    def test_agent_with_two_collections_blocked_from_third(self):
        agent = _FakeAgent(bound_collection_ids=[5, 9], bound_collection_id=5)
        principal = SearchPrincipal(user=object(), agent=agent)
        with pytest.raises(HTTPException) as exc:
            _enforce_agent_collection_scope(principal, collection_id=7)
        assert exc.value.status_code == 403

    def test_reader_does_not_silently_use_only_the_first(self):
        """Regression: membership check, not equality to bound_collection_id."""
        agent = _FakeAgent(bound_collection_ids=[3, 8], bound_collection_id=3)
        principal = SearchPrincipal(user=object(), agent=agent)
        # Second id must be allowed even though the derived single column is 3.
        _enforce_agent_collection_scope(principal, collection_id=8)

    def test_unbound_agent_cannot_search_any_collection(self):
        agent = _FakeAgent(bound_collection_ids=[], bound_collection_id=None)
        principal = SearchPrincipal(user=object(), agent=agent)
        with pytest.raises(HTTPException) as exc:
            _enforce_agent_collection_scope(principal, collection_id=5)
        assert exc.value.status_code == 403

    def test_legacy_single_column_fallback_when_bindings_empty(self):
        # Pre-migration row: column set, junction empty.
        agent = _FakeAgent(bound_collection_ids=[], bound_collection_id=5)
        principal = SearchPrincipal(user=object(), agent=agent)
        _enforce_agent_collection_scope(principal, collection_id=5)
        with pytest.raises(HTTPException):
            _enforce_agent_collection_scope(principal, collection_id=9)

    def test_user_principal_is_unaffected(self):
        principal = SearchPrincipal(user=object(), agent=None)
        _enforce_agent_collection_scope(principal, collection_id=5)
        _enforce_agent_collection_scope(principal, collection_id=999)


# ── request resolution / helpers ─────────────────────────────────────────────


class TestResolveRequestedIds:
    def test_collection_ids_wins_over_single(self):
        assert resolve_requested_collection_ids(
            collection_ids=[9, 3, 9],
            collection_id=1,
            collection_ids_provided=True,
            collection_id_provided=True,
        ) == [3, 9]

    def test_legacy_single_expands(self):
        assert resolve_requested_collection_ids(
            collection_ids=None,
            collection_id=4,
            collection_ids_provided=False,
            collection_id_provided=True,
        ) == [4]

    def test_explicit_null_single_clears(self):
        assert resolve_requested_collection_ids(
            collection_ids=None,
            collection_id=None,
            collection_ids_provided=False,
            collection_id_provided=True,
        ) == []

    def test_neither_means_unchanged(self):
        assert resolve_requested_collection_ids(
            collection_ids=None,
            collection_id=None,
            collection_ids_provided=False,
            collection_id_provided=False,
        ) is None


class TestSetBoundCollectionIds:
    def test_syncs_derived_column_to_min(self, db):
        owner = make_user(db, username="bind-owner", role="developer")
        agent = make_agent(db, owner, name="bind-agent")
        c1 = _collection(db, owner, "gen")
        c2 = _collection(db, owner, "proj")
        # Insert higher id first so derived mirror must sort, not take first write.
        written = set_bound_collection_ids(db, agent, [c2.id, c1.id])
        db.commit()
        db.refresh(agent)
        assert written == sorted([c1.id, c2.id])
        assert get_bound_collection_ids(agent) == written
        assert agent.bound_collection_id == min(c1.id, c2.id)

    def test_empty_clears_bindings_and_column(self, db):
        owner = make_user(db, username="clear-owner", role="developer")
        agent = make_agent(db, owner, name="clear-agent")
        c = _collection(db, owner, "solo")
        set_bound_collection_ids(db, agent, [c.id])
        db.commit()
        set_bound_collection_ids(db, agent, [])
        db.commit()
        db.refresh(agent)
        assert get_bound_collection_ids(agent) == []
        assert agent.bound_collection_id is None


# ── migration backfill ───────────────────────────────────────────────────────


class TestMigrationBackfill:
    def test_existing_single_binding_carried_across(self, db):
        """Simulate r1_0017 backfill INSERT … SELECT without running alembic."""
        owner = make_user(db, username="mig-owner", role="developer")
        agent = make_agent(db, owner, name="mig-agent")
        coll = _collection(db, owner, "legacy-bound")
        # Legacy single-column state: column set, junction empty.
        agent.bound_collection_id = coll.id
        db.commit()
        assert (
            db.query(AgentCollectionBinding)
            .filter(AgentCollectionBinding.agent_id == agent.id)
            .count()
            == 0
        )

        # Same SQL shape as r1_0017 (SQLite-compatible NOT EXISTS).
        db.execute(
            text(
                """
                INSERT INTO agent_collection_bindings (agent_id, collection_id)
                SELECT a.id, a.bound_collection_id
                  FROM agents a
                 WHERE a.bound_collection_id IS NOT NULL
                   AND NOT EXISTS (
                     SELECT 1 FROM agent_collection_bindings b
                      WHERE b.agent_id = a.id
                        AND b.collection_id = a.bound_collection_id
                   )
                """
            )
        )
        db.commit()
        rows = (
            db.query(AgentCollectionBinding)
            .filter(AgentCollectionBinding.agent_id == agent.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].collection_id == coll.id

        # Idempotent re-run does not duplicate.
        db.execute(
            text(
                """
                INSERT INTO agent_collection_bindings (agent_id, collection_id)
                SELECT a.id, a.bound_collection_id
                  FROM agents a
                 WHERE a.bound_collection_id IS NOT NULL
                   AND NOT EXISTS (
                     SELECT 1 FROM agent_collection_bindings b
                      WHERE b.agent_id = a.id
                        AND b.collection_id = a.bound_collection_id
                   )
                """
            )
        )
        db.commit()
        assert (
            db.query(AgentCollectionBinding)
            .filter(AgentCollectionBinding.agent_id == agent.id)
            .count()
            == 1
        )


# ── register / update API ────────────────────────────────────────────────────


class TestRegisterAndUpdateBindings:
    def test_register_accepts_collection_ids_set(self, client, db):
        dev = make_user(db, username="reg-dev", role="developer")
        model = make_model(db, name="reg-model")
        c1 = _collection(db, dev, "reg-a")
        c2 = _collection(db, dev, "reg-b")
        token = login(client, "reg-dev")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "multi-bind-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Handles project and general knowledge",
                "base_model_id": model.id,
                "collection_ids": [c2.id, c1.id],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["bound_collection_ids"] == sorted([c1.id, c2.id])
        assert data["bound_collection_id"] == min(c1.id, c2.id)

        agent = db.query(Agent).filter(Agent.id == data["id"]).one()
        assert get_bound_collection_ids(agent) == sorted([c1.id, c2.id])

    def test_register_legacy_collection_id_expands(self, client, db):
        dev = make_user(db, username="leg-dev", role="developer")
        model = make_model(db, name="leg-model")
        c = _collection(db, dev, "leg-only")
        token = login(client, "leg-dev")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "legacy-bind-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Legacy single collection_id field still works",
                "base_model_id": model.id,
                "collection_id": c.id,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["bound_collection_ids"] == [c.id]
        assert data["bound_collection_id"] == c.id

    def test_register_with_none_behaves_as_unbound(self, client, db):
        dev = make_user(db, username="none-dev", role="developer")
        model = make_model(db, name="none-model")
        token = login(client, "none-dev")

        resp = client.post(
            "/api/agents/register",
            json={
                "name": "unbound-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "Non-RAG agent leaves collections empty",
                "base_model_id": model.id,
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["bound_collection_ids"] == []
        assert data["bound_collection_id"] is None

    def test_update_accepts_set_and_writes_audit(self, client, db):
        owner = make_user(db, username="upd-owner", role="developer")
        model = make_model(db, name="upd-model")
        agent = make_agent(db, owner, name="upd-agent")
        agent.base_model_id = model.id
        db.commit()
        c1 = _collection(db, owner, "upd-a")
        c2 = _collection(db, owner, "upd-b")
        set_bound_collection_ids(db, agent, [c1.id])
        db.commit()
        token = login(client, "upd-owner")

        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c1.id, c2.id]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["bound_collection_ids"] == sorted([c1.id, c2.id])

        audit = (
            db.query(AuditLog)
            .filter(
                AuditLog.action == "update_bound_collections",
                AuditLog.resource_type == "agent",
                AuditLog.resource_id == str(agent.id),
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit is not None
        assert str([c1.id]) in (audit.detail or "") or f"[{c1.id}]" in (audit.detail or "")
        assert (
            str(sorted([c1.id, c2.id])) in (audit.detail or "")
            or f"[{c1.id}, {c2.id}]" in (audit.detail or "")
            or f"[{c2.id}, {c1.id}]" in (audit.detail or "")
        )
        import json

        meta = json.loads(audit.metadata_json or "{}")
        assert meta["from_collection_ids"] == [c1.id]
        assert meta["to_collection_ids"] == sorted([c1.id, c2.id])

    def test_update_auth_matches_agent_editor_rule(self, client, db):
        owner = make_user(db, username="auth-owner", role="developer")
        other = make_user(db, username="auth-other", role="developer")
        model = make_model(db, name="auth-model")
        agent = make_agent(db, owner, name="auth-agent")
        agent.base_model_id = model.id
        db.commit()
        c = _collection(db, owner, "auth-coll")
        # Other developer owns a collection too, but may not edit this agent.
        _collection(db, other, "other-coll")

        other_token = login(client, "auth-other")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c.id]},
            headers=_auth(other_token),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Agent 不存在"

        owner_token = login(client, "auth-owner")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c.id]},
            headers=_auth(owner_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == [c.id]

    def test_admin_cannot_bind_collection_owner_cannot_use(self, client, db):
        """Bind gate uses AGENT OWNER entitlement (same subject as search).

        Previously this test asserted an admin may bind an admin-owned
        collection to a developer's agent. That accepted a binding the
        owner could never search (csk- principal = owner) and bricked the
        owner's console save (which always resends the full set). Both
        faces must refuse collections the owner cannot read.
        """
        owner = make_user(db, username="adm-owner", role="developer")
        admin = make_user(db, username="adm-admin", role="admin")
        model = make_model(db, name="adm-model")
        agent = make_agent(db, owner, name="adm-agent")
        agent.base_model_id = model.id
        db.commit()
        # Collection only the admin can read — owner must not be able to
        # exercise it at search time, so bind must refuse it.
        c_admin = _collection(db, admin, "adm-only-coll")
        token = login(client, "adm-admin")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c_admin.id]},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text
        db.refresh(agent)
        assert get_bound_collection_ids(agent) == []

        # Admin may still bind a collection the owner owns.
        c_owner = _collection(db, owner, "owner-coll")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c_owner.id]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == [c_owner.id]

    def test_owner_unrelated_edit_never_403s_on_preexisting_binding(
        self, client, db
    ):
        """Stale binding must not block an unrelated field edit.

        Transfers the bound collection away from the agent owner so
        ``_validate_collection_access_for_ids(owner, {stale})`` would
        403 — proving the write path must not re-validate unchanged ids.
        """
        owner = make_user(db, username="noop-owner", role="developer")
        other = make_user(db, username="noop-other", role="developer")
        model = make_model(db, name="noop-model")
        agent = make_agent(db, owner, name="noop-agent")
        agent.base_model_id = model.id
        agent.description_for_router = "original description text here"
        db.commit()
        c = _collection(db, owner, "noop-coll")
        set_bound_collection_ids(db, agent, [c.id])
        db.commit()
        _transfer_collection(db, c, other)

        token = login(client, "noop-owner")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={
                "description_for_router": "updated description text here ok",
                "collection_ids": [c.id],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description_for_router"] == (
            "updated description text here ok"
        )
        assert resp.json()["bound_collection_ids"] == [c.id]

    def test_unchanged_collection_ids_resubmission_accepted_by_editor(
        self, client, db
    ):
        """Resubmitting an unchanged set with a stale binding must 200.

        Collection is transferred away from the owner after bind so the
        gate would fail if the whole set were re-validated. Owner and
        admin editors both exercise the same path.
        """
        owner = make_user(db, username="resub-owner", role="developer")
        admin = make_user(db, username="resub-admin", role="admin")
        other = make_user(db, username="resub-other", role="developer")
        model = make_model(db, name="resub-model")
        agent = make_agent(db, owner, name="resub-agent")
        agent.base_model_id = model.id
        db.commit()
        c = _collection(db, owner, "resub-coll")
        set_bound_collection_ids(db, agent, [c.id])
        db.commit()
        _transfer_collection(db, c, other)

        owner_token = login(client, "resub-owner")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c.id]},
            headers=_auth(owner_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == [c.id]

        admin_token = login(client, "resub-admin")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [c.id]},
            headers=_auth(admin_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == [c.id]

    def test_owner_adds_accessible_collection_while_stale_binding_present(
        self, client, db
    ):
        """Stale {1} + add accessible {5} → 200, resulting set {1,5}."""
        owner = make_user(db, username="delta-add-owner", role="developer")
        other = make_user(db, username="delta-add-other", role="developer")
        model = make_model(db, name="delta-add-model")
        agent = make_agent(db, owner, name="delta-add-agent")
        agent.base_model_id = model.id
        db.commit()
        stale = _collection(db, owner, "delta-stale")
        fresh = _collection(db, owner, "delta-fresh")
        set_bound_collection_ids(db, agent, [stale.id])
        db.commit()
        _transfer_collection(db, stale, other)

        token = login(client, "delta-add-owner")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [stale.id, fresh.id]},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == sorted(
            [stale.id, fresh.id]
        )

    def test_owner_removes_stale_binding(self, client, db):
        """Stale {1} removed by submitting {} → 200 (removal needs no access)."""
        owner = make_user(db, username="delta-rm-owner", role="developer")
        other = make_user(db, username="delta-rm-other", role="developer")
        model = make_model(db, name="delta-rm-model")
        agent = make_agent(db, owner, name="delta-rm-agent")
        agent.base_model_id = model.id
        db.commit()
        stale = _collection(db, owner, "delta-rm-stale")
        set_bound_collection_ids(db, agent, [stale.id])
        db.commit()
        _transfer_collection(db, stale, other)

        token = login(client, "delta-rm-owner")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": []},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["bound_collection_ids"] == []

    def test_owner_add_inaccessible_403s_naming_new_not_stale(
        self, client, db
    ):
        """Stale {1} + inaccessible {5} → 403 naming 5, not 1."""
        owner = make_user(db, username="delta-403-owner", role="developer")
        other = make_user(db, username="delta-403-other", role="developer")
        model = make_model(db, name="delta-403-model")
        agent = make_agent(db, owner, name="delta-403-agent")
        agent.base_model_id = model.id
        db.commit()
        stale = _collection(db, owner, "delta-403-stale")
        inaccessible = _collection(db, other, "delta-403-inacc")
        set_bound_collection_ids(db, agent, [stale.id])
        db.commit()
        _transfer_collection(db, stale, other)

        token = login(client, "delta-403-owner")
        resp = client.put(
            f"/api/agents/{agent.id}",
            json={"collection_ids": [stale.id, inaccessible.id]},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json().get("detail", "")
        assert str(inaccessible.id) in detail
        assert f"No access to collection {inaccessible.id}" in detail
        # Must not blame the grandfathered stale id.
        assert f"No access to collection {stale.id}" not in detail
        db.refresh(agent)
        assert get_bound_collection_ids(agent) == [stale.id]
