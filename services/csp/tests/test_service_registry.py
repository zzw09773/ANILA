# -*- coding: utf-8 -*-
"""Slice 7a — Service Registry / Launch Gateway tests (doc 07).

Covers: migration single head + data-migration fidelity (platform_link →
registered_services with grants intact), the 8-step access algorithm matrix,
launch happy/deny paths (JWKS-verifiable token, 14 claims, TTL bounds,
service_launches row + PolicyDecision + audit), audit callback auth + payload
bounds, and the /api/platform-links compat façade.
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
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.services import access_control, agent_credential_service
from app.services.agent_credential_service import CallerIdentity
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
    def test_single_head_in_r1_namespace(self):
        # 不釘死特定 head id(每加一個 migration 就過期,如 Slice 8a r1_0007);
        # 守住兩個不變量:恰一個 head + head 屬 r1_ 命名空間(對齊
        # tests/test_task_trace_schema.py 的 invariant 風格)。
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # 路徑一律從本檔推出來,不從 cwd 推。原本的 ``Config("alembic.ini")``
        # 只有 cwd == services/csp 時才找得到檔;從 repo 根目錄跑就紅一支,
        # 這是「基準線隨目錄改變」的其中一條。``script_location`` 也要一起覆寫
        # —— alembic 是拿 cwd 解析那個相對路徑的。
        csp_root = Path(__file__).resolve().parents[1]
        cfg = Config(str(csp_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(csp_root / "migrations"))
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())
        assert len(heads) == 1, f"alembic head 應唯一,實得 {heads}"
        assert heads[0].startswith("r1_"), (
            f"head 應屬 r1_ 命名空間,實得 {heads}"
        )

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

    def test_platform_link_migrates_with_grant_intact(self, db_engine):
        Session = sessionmaker(bind=db_engine, expire_on_commit=False)
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
        assert svc.config_source == "db"
        grant = s2.get(ServiceAccessGrant, grant_id)
        assert grant is not None  # grant history survives
        assert grant.service_id == link_id  # backfilled
        assert grant.platform_link_id == link_id
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
        svc = _make_service(db, is_public=True, classification_ceiling="密")
        # launch context above ceiling → hard deny for ALL tiers.
        assert (
            access_control.can_access_service(
                db, admin, svc, context_level="機密"
            )
            is False
        )
        # at/under ceiling → allowed.
        assert (
            access_control.can_access_service(db, admin, svc, context_level="密")
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
        assert resp.status_code == 404
        assert resp.json()["detail"] == "服務不存在"
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

    def test_launch_rejects_source_snapshot_owned_by_another_user(self, client, db):
        victim = make_user(db, username="snapshot-owner")
        task = Task(
            title="private task",
            task_type="query",
            requester_user_id=victim.id,
            status="completed",
        )
        db.add(task)
        db.commit()
        snapshot = SourceSnapshot(task_id=task.id, origin="upload", source_scope="personal")
        db.add(snapshot)
        db.commit()

        headers = _auth_headers(client, db, username="attacker")
        svc = _make_service(db, slug="snapshot-svc", name="Snapshot Svc", is_public=True)
        resp = client.post(
            f"/api/services/{svc.slug}/launch",
            json={"source_snapshot_id": snapshot.id},
            headers=headers,
        )
        assert resp.status_code == 403, resp.text
        assert db.query(ServiceLaunch).count() == 0

    def test_launch_rejects_non_http_entry_url_before_issuing_token(self, client, db):
        headers = _auth_headers(client, db, username="launch-user")
        svc = _make_service(
            db,
            slug="unsafe-url-svc",
            name="Unsafe URL",
            is_public=True,
            entry_url="javascript:alert(1)",
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "entry_url" in resp.text
        assert db.query(ServiceLaunch).count() == 0

    def test_launch_rejects_invalid_port_before_issuing_token(self, client, db):
        headers = _auth_headers(client, db, username="launch-bad-port")
        svc = _make_service(
            db,
            slug="bad-port-svc",
            name="Bad Port",
            is_public=True,
            entry_url="https://example.com:99999/app",
        )
        resp = client.post(f"/api/services/{svc.slug}/launch", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "entry_url" in resp.text
        assert db.query(ServiceLaunch).count() == 0


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
        sc = self._setup(db, monkeypatch)
        # R-SEC (ADR-0008): the service must be bound to the presenting client.
        svc = _make_service(db, service_client_id=sc.id)
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
        sc = self._setup(db, monkeypatch)
        svc = _make_service(db, service_client_id=sc.id)  # bound → reaches size gate
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={
                "event_type": "analysis.completed",
                "metadata": {"blob": "x" * 20000},
            },
            headers={"Authorization": f"Bearer {self._GOOD}"},
        )
        assert resp.status_code == 413

    # ── R-SEC (ADR-0008): fail-closed client↔service binding ─────────────────

    def test_unbound_service_rejected_403(self, client, db, monkeypatch):
        """A service with NULL binding rejects ALL callbacks — even with a
        valid Service Client Token (fail-closed default-deny)."""
        import json as _json

        from app.models.service_launch import ServiceAuditCallback

        self._setup(db, monkeypatch)
        svc = _make_service(db)  # service_client_id is NULL → unbound
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={"event_type": "session.started"},
            headers={"Authorization": f"Bearer {self._GOOD}"},
        )
        assert resp.status_code == 403, resp.text
        assert "尚未綁定" in resp.json()["detail"]
        # no callback row appended …
        assert db.query(ServiceAuditCallback).count() == 0
        # … and a denied audit row records the attempt.
        audit = (
            db.query(AuditLog)
            .filter_by(action="service.audit_callback", status="denied")
            .one()
        )
        assert _json.loads(audit.metadata_json)["reason"] == "attempted_unbound_callback"

    def test_cross_service_mismatch_403(self, client, db, monkeypatch):
        """A token valid for client A cannot post to a service bound to a
        DIFFERENT client B (cross-service audit-trail pollution)."""
        import json as _json

        from app.models.service_launch import ServiceAuditCallback

        sc_a = ServiceClient(
            client_name="svc-a", client_type="worker",
            service_token_envelope="enc::a", service_token_lookup_hash="a" * 64,
            is_active=True,
        )
        sc_b = ServiceClient(
            client_name="svc-b", client_type="worker",
            service_token_envelope="enc::b", service_token_lookup_hash="b" * 64,
            is_active=True,
        )
        db.add_all([sc_a, sc_b])
        db.commit()
        db.refresh(sc_a)
        db.refresh(sc_b)

        def _fake_verify(_db, *, token):
            if token != "csk-token-A":
                return None
            return CallerIdentity(
                kind="service_client", agent_id=None, service_client_id=sc_a.id,
                credential_id=sc_a.id, is_legacy=False, used_previous_token=False,
            )

        monkeypatch.setattr(
            agent_credential_service, "verify_service_token", _fake_verify
        )
        # service bound to B; caller presents A's token → mismatch.
        svc = _make_service(db, service_client_id=sc_b.id)
        resp = client.post(
            f"/api/services/{svc.slug}/audit-callbacks",
            json={"event_type": "session.started"},
            headers={"Authorization": "Bearer csk-token-A"},
        )
        assert resp.status_code == 403, resp.text
        assert db.query(ServiceAuditCallback).count() == 0
        audit = (
            db.query(AuditLog)
            .filter_by(action="service.audit_callback", status="denied")
            .one()
        )
        meta = _json.loads(audit.metadata_json)
        assert meta["reason"] == "attempted_cross_service_callback"
        assert meta["bound_client_id"] == sc_b.id
        assert meta["presented_client_id"] == sc_a.id


# ── R-SEC binding governance (ADR-0008): who may set the binding ─────────────


class TestAuditCallbackBindingGovernance:
    """Admin-tier owns the client↔service binding; a per-service admin may NOT
    self-bind — binding grants audit-write identity, so delegation must not
    self-serve (ADR-0008)."""

    def _client_row(self, db, name="bind-target") -> ServiceClient:
        sc = ServiceClient(
            client_name=name,
            client_type="worker",
            service_token_envelope="enc::stub",
            service_token_lookup_hash=(name[:1] * 64),
            is_active=True,
        )
        db.add(sc)
        db.commit()
        db.refresh(sc)
        return sc

    def test_admin_can_set_binding(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        sc = self._client_row(db)
        svc = _make_service(db)
        resp = client.put(
            f"/api/services/{svc.slug}",
            json={"service_client_id": sc.id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["service_client_id"] == sc.id
        db.refresh(svc)
        assert svc.service_client_id == sc.id

    def test_admin_can_set_binding_at_create(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        sc = self._client_row(db, name="c")
        resp = client.post(
            "/api/services",
            json={"name": "bound-svc", "entry_url": "https://b.local",
                  "service_client_id": sc.id},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["service_client_id"] == sc.id

    def test_per_service_admin_cannot_set_binding(self, client, db):
        deleg = make_user(db, username="deleg", role="user")
        sc = self._client_row(db, name="d")
        # Even with service_client_id whitelisted in db_editable_fields, the
        # per-service admin is blocked (admin-only field takes precedence).
        svc = _make_service(
            db,
            service_admin_user_ids=[deleg.id],
            db_editable_fields=["service_client_id"],
        )
        token = login(client, username="deleg")
        resp = client.put(
            f"/api/services/{svc.slug}",
            json={"service_client_id": sc.id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, resp.text
        db.refresh(svc)
        assert svc.service_client_id is None  # binding unchanged


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


# ── registry write contract (entry_url; url is not an alias) ───────────────


class TestRegistryWriteContract:
    """Governance UI must send entry_url. extra=forbid makes a leftover ``url``
    key loud (422) instead of silently dropping the address on update."""

    def test_create_with_entry_url_persists(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        resp = client.post(
            "/api/services",
            json={"name": "Studio", "entry_url": "https://studio.local/app"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["entry_url"] == "https://studio.local/app"
        svc = db.get(RegisteredService, body["id"])
        assert svc is not None and svc.entry_url == "https://studio.local/app"

    def test_create_with_legacy_url_key_is_422(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        resp = client.post(
            "/api/services",
            json={"name": "Studio", "url": "https://studio.local/app"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text

    def test_update_entry_url_persists(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        svc = _make_service(db, entry_url="https://old.local")
        resp = client.put(
            f"/api/services/{svc.slug}",
            json={"entry_url": "https://new.local/path"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["entry_url"] == "https://new.local/path"
        db.refresh(svc)
        assert svc.entry_url == "https://new.local/path"

    def test_update_with_legacy_url_key_is_422_and_leaves_entry_url(self, client, db):
        headers = _auth_headers(client, db, username="root", role="admin")
        svc = _make_service(db, entry_url="https://keep.local")
        resp = client.put(
            f"/api/services/{svc.slug}",
            json={"url": "https://dropped.local", "name": "renamed"},
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        db.refresh(svc)
        assert svc.entry_url == "https://keep.local"
        assert svc.name != "renamed"  # whole body rejected; no partial apply
