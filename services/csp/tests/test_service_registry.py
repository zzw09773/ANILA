# -*- coding: utf-8 -*-
"""Slice 7a — Service Registry / Launch Gateway tests (doc 07).

Covers: migration single head + data-migration fidelity (platform_link →
registered_services with grants intact), the 8-step access algorithm matrix,
launch happy/deny paths (JWKS-verifiable token, 14 claims, TTL bounds,
service_launches row + PolicyDecision + audit), audit callback auth + payload
bounds, the /api/platform-links compat façade, and the config_source seed rework.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from jose import jwt
from sqlalchemy.orm import sessionmaker

from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.platform_link import PlatformLink
from app.models.policy_decision import PolicyDecision
from app.models.registered_service import RegisteredService
from app.models.service_access_grant import ServiceAccessGrant
from app.models.service_client import ServiceClient
from app.models.service_launch import ServiceLaunch
from app.services import access_control, agent_credential_service
from app.services.agent_credential_service import CallerIdentity
from app.services.auto_seed import sync_env_seeded_services
from app.services.registry_backfill import backfill_registered_services
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_service(db, *, name="材料分析", slug="material-analysis", **kw) -> RegisteredService:
    svc = RegisteredService(
        name=name,
        slug=slug,
        entry_url=kw.pop("entry_url", "https://material.local/app"),
        **kw,
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return svc


def _auth_headers(client, db, username="alice", role="user") -> dict:
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


# ── migration head ──────────────────────────────────────────────────────────


class TestMigration:
    def test_single_head_r1_0006(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        assert list(script.get_heads()) == ["r1_0006"]

    def test_down_revision_chains_r1_0005(self):
        from migrations.versions import r1_0006_service_registry as mig

        assert mig.revision == "r1_0006"
        assert mig.down_revision == "r1_0005"


# ── data-migration fidelity ─────────────────────────────────────────────────


class TestDataMigration:
    def _seed_link_and_grant(self, session):
        user = make_user(session, username="grantee")
        link = PlatformLink(
            name="ANILA LM",
            url="https://anila.local/anilalm",
            icon="book",
            description="KB",
            sort_order=3,
            is_active=True,
            is_public=False,
            required_roles=["admin"],
        )
        session.add(link)
        session.commit()
        grant = ServiceAccessGrant(
            user_id=user.id, platform_link_id=link.id, granted_by=user.id
        )
        session.add(grant)
        session.commit()
        return link.id, grant.id

    def test_platform_link_migrates_with_grant_intact(self, db_engine, monkeypatch):
        monkeypatch.setenv("AUTO_REGISTER_LINKS", "[]")  # ANILA LM not in env → db
        Session = sessionmaker(bind=db_engine)
        s = Session()
        link_id, grant_id = self._seed_link_and_grant(s)
        s.close()

        created = backfill_registered_services(db_engine)
        assert created == 1

        s2 = Session()
        svc = s2.get(RegisteredService, link_id)
        assert svc is not None
        assert svc.id == link_id  # id mirrored 1:1
        assert svc.name == "ANILA LM"
        assert svc.entry_url == "https://anila.local/anilalm"  # url → entry_url
        assert svc.sort_order == 3
        assert svc.required_roles == ["admin"]
        assert svc.allowed_origins == ["https://anila.local"]  # backfilled origin
        assert svc.config_source == "db"  # not in AUTO_REGISTER_LINKS
        grant = s2.get(ServiceAccessGrant, grant_id)
        assert grant is not None  # grant history survives
        assert grant.service_id == link_id  # backfilled
        assert grant.platform_link_id == link_id
        s2.close()

    def test_env_seeded_detection_and_idempotent(self, db_engine, monkeypatch):
        import json

        monkeypatch.setenv(
            "AUTO_REGISTER_LINKS",
            json.dumps([{"name": "ANILA LM", "url": "x"}]),
        )
        Session = sessionmaker(bind=db_engine)
        s = Session()
        self._seed_link_and_grant(s)
        s.close()

        assert backfill_registered_services(db_engine) == 1
        # second run is a no-op (idempotent) — no duplicate row
        assert backfill_registered_services(db_engine) == 0

        s2 = Session()
        svc = s2.query(RegisteredService).filter_by(name="ANILA LM").one()
        assert svc.config_source == "env_seeded"
        assert svc.env_seed_key == "ANILA LM"
        assert svc.db_editable_fields == ["is_active"]
        assert svc.last_seeded_at is not None
        s2.close()


# ── access algorithm matrix (doc §12) ───────────────────────────────────────


class TestAccessAlgorithm:
    def test_public_service_visible_to_regular_user(self, db):
        user = make_user(db)
        svc = _make_service(db, is_public=True)
        assert access_control.can_access_service(db, user, svc) is True

    def test_role_gate_denies(self, db):
        user = make_user(db, role="user")
        svc = _make_service(db, is_public=True, required_roles=["admin"])
        assert access_control.can_access_service(db, user, svc) is False

    def test_user_grant_allows(self, db):
        user = make_user(db)
        svc = _make_service(db, is_public=False)
        db.add(ServiceAccessGrant(user_id=user.id, service_id=svc.id))
        db.commit()
        assert access_control.can_access_service(db, user, svc) is True

    def test_department_grant_allows(self, db):
        dept = Department(name="材料組")
        db.add(dept)
        db.commit()
        user = make_user(db)
        user.department_id = dept.id
        db.commit()
        svc = _make_service(db, is_public=False)
        db.add(ServiceAccessGrant(department_id=dept.id, service_id=svc.id))
        db.commit()
        assert access_control.can_access_service(db, user, svc) is True

    def test_default_deny_private_no_grant(self, db):
        user = make_user(db)
        svc = _make_service(db, is_public=False)
        assert access_control.can_access_service(db, user, svc) is False

    def test_admin_bypass_sees_private(self, db):
        admin = make_user(db, username="root", role="admin")
        svc = _make_service(db, is_public=False)
        assert access_control.can_access_service(db, admin, svc) is True

    def test_classification_clearance_denies_even_admin(self, db):
        admin = make_user(db, username="root", role="admin")
        svc = _make_service(db, is_public=True, classification_ceiling="機密")
        # launch context above ceiling → hard deny for ALL tiers.
        assert (
            access_control.can_access_service(
                db, admin, svc, context_level="極機密"
            )
            is False
        )
        # at/under ceiling → allowed.
        assert (
            access_control.can_access_service(db, admin, svc, context_level="機密")
            is True
        )

    def test_migrated_grant_matched_by_platform_link_id(self, db):
        # grant keyed only by the legacy platform_link_id still counts.
        user = make_user(db)
        svc = _make_service(db, is_public=False)
        db.add(
            ServiceAccessGrant(user_id=user.id, platform_link_id=svc.id)
        )
        db.commit()
        assert access_control.can_access_service(db, user, svc) is True


# ── launch gateway ──────────────────────────────────────────────────────────


class TestLaunch:
    def _jwks_key(self, client) -> dict:
        return client.get("/.well-known/jwks.json").json()["keys"][0]

    def test_launch_happy_path(self, client, db):
        headers = _auth_headers(client, db, username="alice")
        svc = _make_service(
            db, is_public=True, supports_launch_token=True, launch_mode="iframe"
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "iframe"
        assert "launch_token" in body["launch_url"]

        # token verifies against the published JWKS with aud/iss.
        claims = jwt.decode(
            body["launch_token"],
            self._jwks_key(client),
            algorithms=["RS256"],
            audience=svc.slug,
            issuer="anila-csp",
        )
        expected = {
            "iss", "aud", "launch_id", "service_id", "user_id", "employee_id",
            "department_id", "roles", "task_id", "trace_id",
            "classification_level", "source_snapshot_id", "iat", "exp",
        }
        assert expected.issubset(claims.keys())
        assert claims["iss"] == "anila-csp"
        assert claims["aud"] == svc.slug
        assert claims["launch_id"] == body["launch_id"]
        # TTL within doc §6 5–10 min (we mint the 10-min upper bound).
        ttl = claims["exp"] - claims["iat"]
        assert 300 <= ttl <= 600
        assert ttl == 600
        # no forbidden claims (doc §6): no model key, no long-lived user JWT.
        assert "api_key" not in claims and "access_token" not in claims

        # server-side records: launch row + PolicyDecision(allow) + audit.
        launch = db.query(ServiceLaunch).filter_by(launch_id=body["launch_id"]).one()
        assert launch.service_id == svc.id and launch.status == "issued"
        pd = (
            db.query(PolicyDecision)
            .filter_by(action="service.launch", decision="allow")
            .first()
        )
        assert pd is not None
        assert (
            db.query(AuditLog)
            .filter_by(action="service.launch", status="success")
            .first()
            is not None
        )

    def test_launch_deny_no_grant(self, client, db):
        headers = _auth_headers(client, db, username="bob")
        svc = _make_service(db, slug="private-svc", name="私有", is_public=False)
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 403
        assert "launch_token" not in resp.json()
        # deny path issues no launch row and records a deny PolicyDecision.
        assert db.query(ServiceLaunch).count() == 0
        assert (
            db.query(PolicyDecision)
            .filter_by(action="service.launch", decision="deny")
            .first()
            is not None
        )

    def test_launch_unauthenticated_401(self, client, db):
        svc = _make_service(db, is_public=True)
        assert client.post(f"/api/services/{svc.slug}/launch", json={}).status_code == 401


# ── audit callback (doc §10) ────────────────────────────────────────────────


class TestAuditCallback:
    """Bearer = Service Client Token (doc 03). The AES envelope crypto needs a
    process-global SECRET_KEY whose presence would leak into other test modules
    and shift the baseline, so we monkeypatch the real verifier (the endpoint
    calls ``agent_credential_service.verify_service_token``) and back it with a
    real ServiceClient row for the integration_key_id FK."""

    _GOOD = "csk-good-integration-key"

    def _setup(self, db, monkeypatch) -> ServiceClient:
        sc = ServiceClient(
            client_name="material-svc",
            client_type="worker",
            service_token_envelope="enc::stub",
            service_token_lookup_hash="0" * 64,
            is_active=True,
        )
        db.add(sc)
        db.commit()
        db.refresh(sc)

        def _fake_verify(_db, *, token):
            if token != self._GOOD:
                return None
            return CallerIdentity(
                kind="service_client",
                agent_id=None,
                service_client_id=sc.id,
                credential_id=sc.id,
                is_legacy=False,
                used_previous_token=False,
            )

        monkeypatch.setattr(
            agent_credential_service, "verify_service_token", _fake_verify
        )
        return sc

    def test_valid_key_appends_row(self, client, db, monkeypatch):
        self._setup(db, monkeypatch)
        svc = _make_service(db)
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={
                "event_type": "analysis.completed",
                "launch_id": "launch_abc",
                "trace_id": "trace_abc",
                "actor": {"employee_id": "123456"},
                "classification_level": "機密",
            },
            headers={"Authorization": f"Bearer {self._GOOD}"},
        )
        assert resp.status_code == 201, resp.text
        from app.models.service_launch import ServiceAuditCallback

        row = db.query(ServiceAuditCallback).filter_by(service_id=svc.id).one()
        assert row.event_type == "analysis.completed"
        assert row.integration_key_id is not None

    def test_bad_key_401(self, client, db, monkeypatch):
        self._setup(db, monkeypatch)
        svc = _make_service(db)
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={"event_type": "session.started"},
            headers={"Authorization": "Bearer csk-not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_bad_event_type_422(self, client, db, monkeypatch):
        self._setup(db, monkeypatch)
        svc = _make_service(db)
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={"event_type": "Session Started!"},  # spaces/uppercase → invalid
            headers={"Authorization": f"Bearer {self._GOOD}"},
        )
        assert resp.status_code == 422

    def test_oversized_payload_413(self, client, db, monkeypatch):
        self._setup(db, monkeypatch)
        svc = _make_service(db)
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={
                "event_type": "analysis.completed",
                "metadata": {"blob": "x" * 20000},
            },
            headers={"Authorization": f"Bearer {self._GOOD}"},
        )
        assert resp.status_code == 413


# ── compat façade (/api/platform-links unchanged shape) ─────────────────────


class TestCompatFacade:
    def test_create_and_list_shape(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        resp = client.post(
            "/api/platform-links",
            json={
                "name": "GitLab",
                "url": "https://gitlab.local",
                "icon": "git",
                "sort_order": 2,
                "is_public": True,
                "required_roles": ["developer"],
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # response keeps the legacy PlatformLinkResponse shape.
        assert set(body) == {
            "id", "name", "url", "icon", "description", "sort_order",
            "is_active", "is_public", "required_roles", "created_at",
        }
        assert body["url"] == "https://gitlab.local"  # entry_url mirrored to url
        # backing row is a RegisteredService authored as config_source=db.
        svc = db.query(RegisteredService).filter_by(name="GitLab").one()
        assert svc.config_source == "db" and svc.entry_url == "https://gitlab.local"

        listing = client.get("/api/platform-links", headers=headers).json()
        assert any(x["name"] == "GitLab" for x in listing)

    def test_update_and_grant_over_registry(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        lid = client.post(
            "/api/platform-links",
            json={"name": "n8n", "url": "https://n8n.local"},
            headers=headers,
        ).json()["id"]
        # PUT maps url → entry_url.
        client.put(
            f"/api/platform-links/{lid}",
            json={"url": "https://n8n.local/new"},
            headers=headers,
        )
        assert db.get(RegisteredService, lid).entry_url == "https://n8n.local/new"
        # grant over the registry service via the compat grants endpoint.
        target = make_user(db, username="carol")
        gresp = client.post(
            "/api/service-access-grants",
            json={"user_id": target.id, "platform_link_id": lid},
            headers=headers,
        )
        assert gresp.status_code == 201, gresp.text
        assert gresp.json()["service_id"] == lid


# ── seed rework (config_source rules) ───────────────────────────────────────


class TestSeedRework:
    def test_env_seed_keeps_admin_sticky_and_resyncs(self, db):
        cfg = [{"name": "GitLab", "url": "https://gitlab.local", "is_public": True}]
        sync_env_seeded_services(db, cfg)
        db.commit()
        svc = db.query(RegisteredService).filter_by(name="GitLab").one()
        assert svc.config_source == "env_seeded"

        # admin edits: deactivate (is_active is admin-sticky) — env changes url.
        svc.is_active = False
        db.commit()
        cfg[0]["url"] = "https://gitlab.local/moved"
        sync_env_seeded_services(db, cfg)
        db.commit()
        db.refresh(svc)
        assert svc.is_active is False  # admin-sticky field NOT clobbered
        assert svc.entry_url == "https://gitlab.local/moved"  # env re-synced

    def test_db_service_never_clobbered(self, db):
        # a UI-authored service sharing the name is left untouched by the seed.
        _make_service(
            db,
            name="GitLab",
            slug="gitlab-db",
            entry_url="https://gitlab.db-owned",
            config_source="db",
        )
        sync_env_seeded_services(
            db, [{"name": "GitLab", "url": "https://gitlab.env"}]
        )
        db.commit()
        svc = db.query(RegisteredService).filter_by(name="GitLab").one()
        assert svc.config_source == "db"
        assert svc.entry_url == "https://gitlab.db-owned"  # untouched
