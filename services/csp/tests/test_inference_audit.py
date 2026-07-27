"""Inference audit trail: real-IP resolver, four entrances, admin query/export."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from app.models.audit_log import AuditLog
from app.models.agent import UserAgentPermission
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.modules.clearance.service import (
    grant_collection_access,
    issue_clearance_grant,
)
from app.services.auth_service import create_tokens
from app.services.client_ip import resolve_client_ip
from app.services.inference_audit import should_record_end_user_inference
from tests.conftest import make_agent, make_model, make_user


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _grant_inference_audit_viewer(db: Session, user) -> None:
    user.can_view_inference_audit = True
    db.commit()
    db.refresh(user)


def _make_request(
    *,
    peer: str | None = "203.0.113.10",
    xff: str | None = None,
) -> StarletteRequest:
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 12345) if peer else None,
        "server": ("testserver", 80),
    }
    return StarletteRequest(scope)


# ── Resolver ─────────────────────────────────────────────────────────────────


def test_resolver_default_ignores_xff(monkeypatch):
    monkeypatch.setattr(
        "app.services.client_ip.settings.ANILA_TRUSTED_PROXY_CIDRS", ""
    )
    req = _make_request(peer="10.0.0.5", xff="1.2.3.4, 10.0.0.5")
    assert resolve_client_ip(req) == "10.0.0.5"


def test_resolver_trusted_peer_takes_rightmost_non_trusted(monkeypatch):
    monkeypatch.setattr(
        "app.services.client_ip.settings.ANILA_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8,192.168.0.0/16",
    )
    req = _make_request(
        peer="10.0.0.1",
        xff="198.51.100.7, 10.0.0.2, 192.168.1.1",
    )
    assert resolve_client_ip(req) == "198.51.100.7"


def test_resolver_spoofed_xff_from_untrusted_peer_ignored(monkeypatch):
    monkeypatch.setattr(
        "app.services.client_ip.settings.ANILA_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8",
    )
    req = _make_request(peer="203.0.113.50", xff="1.1.1.1, 10.0.0.9")
    assert resolve_client_ip(req) == "203.0.113.50"


def test_resolver_malformed_xff_safe(monkeypatch):
    monkeypatch.setattr(
        "app.services.client_ip.settings.ANILA_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8",
    )
    req = _make_request(peer="10.0.0.1", xff="not-an-ip, ;;; , 198.51.100.9")
    assert resolve_client_ip(req) == "198.51.100.9"


def test_internal_router_and_service_hops_skip_audit():
    req = _make_request()
    assert should_record_end_user_inference(req, internal_router=True) is False
    req.state.csp_caller = object()
    assert should_record_end_user_inference(req, internal_router=False) is False
    req2 = _make_request()
    req2.state.suppress_inference_audit = True
    assert should_record_end_user_inference(req2, internal_router=False) is False
    assert should_record_end_user_inference(_make_request()) is True


# ── Entrances ────────────────────────────────────────────────────────────────


@pytest.fixture
def _mock_upstream(monkeypatch):
    """Avoid real HTTP to mock-llm / agents during chat/agent tests."""
    from app.services import proxy_service

    class _PostResponse:
        def __init__(self, payload, status_code: int = 200):
            self.status_code = status_code
            self.headers = {"content-type": "application/json"}
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _PostResponse(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            )

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _FakeClient(*a, **k)
    )


def _make_collection(db, *, name: str, owner_id: int) -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        created_by=owner_id,
        status="active",
        chunking_config={"strategy": "semantic"},
        embedding_model="nv-embed",
        embedding_dim=4000,
        embedding_fingerprint="sha256:" + "0" * 64,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def test_chat_entrance_writes_one_row(
    client: TestClient, db: Session, monkeypatch, _mock_upstream
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_chat_user", role="admin")
    make_model(db, name="gpt-audit-chat")
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-audit-chat",
            "messages": [{"role": "user", "content": "what is the status?"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200, resp.text
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "inference.chat")
        .order_by(AuditLog.id)
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_username == "audit_chat_user"
    assert row.actor_user_id == user.id
    assert row.resource_type == "inference"
    assert row.resource_id == "gpt-audit-chat"
    assert row.detail == "what is the status?"
    assert row.status == "success"
    assert row.ip_address is not None


def test_agent_entrance_via_public_chat_writes_inference_agent(
    client: TestClient, db: Session, monkeypatch, _mock_upstream
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    monkeypatch.setattr(
        "app.services.proxy.service.settings.ALLOW_LEGACY_AGENT_DISPATCH", True
    )
    monkeypatch.setattr(
        "app.config.settings.ALLOW_LEGACY_AGENT_DISPATCH", True
    )
    user = make_user(db, username="audit_agent_user")
    owner = make_user(db, username="audit_agent_owner", role="developer")
    agent = make_agent(
        db, owner, name="audit-agent-1", approval_status="approved"
    )
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "audit-agent-1",
            "messages": [{"role": "user", "content": "dispatch me please"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200, resp.text
    agent_rows = (
        db.query(AuditLog).filter(AuditLog.action == "inference.agent").all()
    )
    chat_rows = (
        db.query(AuditLog).filter(AuditLog.action == "inference.chat").all()
    )
    assert len(agent_rows) == 1
    assert len(chat_rows) == 0
    assert agent_rows[0].detail == "dispatch me please"
    assert agent_rows[0].resource_id == "audit-agent-1"
    assert agent_rows[0].actor_username == "audit_agent_user"


def test_formal_agent_chat_writes_one_denied_row(
    client: TestClient, db: Session, monkeypatch
):
    """Gate 5 formal: approved agent via /v1/chat → 409 + exactly one denied row."""
    monkeypatch.setattr(
        "app.api.proxy.settings.GATE5_MODEL_GOVERNANCE_ENABLED", True
    )
    monkeypatch.setattr(
        "app.api.proxy.settings.ANILA_DEPLOYMENT_PROFILE", "development"
    )
    user = make_user(db, username="audit_formal_agent_user")
    owner = make_user(db, username="audit_formal_agent_owner", role="developer")
    agent = make_agent(
        db, owner, name="audit-formal-agent", approval_status="approved"
    )
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "audit-formal-agent",
            "messages": [{"role": "user", "content": "formal should deny"}],
            "stream": False,
        },
    )
    assert resp.status_code == 409, resp.text
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "inference.agent")
        .order_by(AuditLog.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "formal should deny"
    assert rows[0].resource_id == "audit-formal-agent"
    assert rows[0].actor_username == "audit_formal_agent_user"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "formal_agent_dispatch_rejected"
    assert meta.get("route_type") == "agent"


def test_formal_agent_chat_service_hop_writes_nothing(
    db: Session, monkeypatch
):
    """Service-hop formal agent rejection must not write an inference.agent row."""
    import asyncio
    from fastapi import HTTPException
    from app.api import proxy as proxy_api
    from app.middleware.caller import Caller

    monkeypatch.setattr(proxy_api.settings, "GATE5_MODEL_GOVERNANCE_ENABLED", True)
    monkeypatch.setattr(proxy_api.settings, "ANILA_DEPLOYMENT_PROFILE", "development")

    user = make_user(db, username="audit_formal_hop_user")
    owner = make_user(db, username="audit_formal_hop_owner", role="developer")
    agent = make_agent(
        db, owner, name="audit-formal-hop-agent", approval_status="approved"
    )
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    body = {
        "model": "audit-formal-hop-agent",
        "messages": [{"role": "user", "content": "hop must not audit"}],
        "stream": False,
    }
    body_bytes = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    req = StarletteRequest(scope, receive)
    # Non-agent service identity: verified-agent helper no-ops; end-user audit gate skips.
    req.state.csp_caller = SimpleNamespace(kind="service", agent_id=None)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            proxy_api._chat_completions_impl(
                req,
                caller=Caller(user=user, api_key_id=None),
                db=db,
                internal_router=False,
            )
        )
    assert caught.value.status_code == 409
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.agent").all()
    assert rows == []


@pytest.mark.asyncio
async def test_formal_session_resume_writes_denied_partial_row(
    db: Session, monkeypatch
):
    """Formal resume rejects before body parse → denied row with metadata.partial."""
    from fastapi import HTTPException
    from app.api import proxy as proxy_api
    from app.middleware.caller import Caller

    monkeypatch.setattr(proxy_api.settings, "GATE5_MODEL_GOVERNANCE_ENABLED", True)
    monkeypatch.setattr(proxy_api.settings, "ANILA_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setattr(proxy_api.settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    monkeypatch.setattr(proxy_api.settings, "ANILA_PILOT_MODE", False)

    user = make_user(db, username="audit_formal_resume_user")
    owner = make_user(db, username="audit_formal_resume_owner", role="developer")
    agent = make_agent(
        db, owner, name="audit-formal-resume-agent", approval_status="approved"
    )
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    async def receive():
        raise AssertionError("formal resume must not read body before reject")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/v1/agents/{agent.name}/sessions/sess-formal/answer",
        "raw_path": b"/answer",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    req = StarletteRequest(scope, receive)

    with pytest.raises(HTTPException) as caught:
        await proxy_api.resume_agent_session(
            agent.name,
            "sess-formal",
            request=req,
            caller=Caller(user=user, api_key_id=None),
            db=db,
        )
    assert caught.value.status_code == 409
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.agent").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail is None
    assert rows[0].resource_id == "audit-formal-resume-agent"
    assert rows[0].actor_username == "audit_formal_resume_user"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "formal_agent_dispatch_rejected"
    assert meta.get("partial") is True
    assert meta.get("session_id") == "sess-formal"


def test_rag_entrance_writes_one_row(client: TestClient, db: Session, monkeypatch):
    import app.api.ingestion.search as search_mod

    user = make_user(db, username="audit_rag_user")
    admin = make_user(db, username="audit_rag_admin", role="admin")
    coll = _make_collection(db, name="audit-rag-coll", owner_id=user.id)

    now = datetime.now(timezone.utc)
    grant = issue_clearance_grant(
        db,
        actor=admin,
        subject_user_id=user.id,
        max_classification_level="絕對機密",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        basis_ticket="AUDIT-RAG",
    )
    grant_collection_access(
        db,
        actor=admin,
        clearance_grant_id=grant.id,
        collection_id=coll.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="AUDIT-NTK",
    )
    db.commit()

    monkeypatch.setattr(
        search_mod, "_embed_query", AsyncMock(return_value=[0.0] * 8)
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: SimpleNamespace())

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        async def similarity_search_scoped_documents(self, **kwargs):
            return []

        async def similarity_search_per_document_authorized(self, **kwargs):
            return []

    monkeypatch.setattr(search_mod, "CollectionScopedPgVectorStore", _FakeStore)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        headers=_bearer(user),
        json={"query": "find the briefing notes", "top_k": 5},
    )
    assert resp.status_code == 200, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].detail == "find the briefing notes"
    assert rows[0].resource_id == str(coll.id)
    assert rows[0].actor_username == "audit_rag_user"


def test_studio_entrance_via_generate_artifact_task(
    client: TestClient, db: Session
):
    user = make_user(db, username="audit_studio_user")
    resp = client.post(
        "/api/tasks",
        headers=_bearer(user),
        json={
            "title": "Make slides about radar coverage",
            "task_type": "generate_artifact",
            "requested_output_type": "slides",
        },
    )
    assert resp.status_code == 201, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.studio").all()
    assert len(rows) == 1
    assert rows[0].detail == "Make slides about radar coverage"
    assert rows[0].actor_username == "audit_studio_user"
    assert rows[0].resource_type == "inference"


@pytest.mark.asyncio
async def test_agent_csk_search_hop_writes_no_rag_audit(db: Session, monkeypatch):
    """Agent-principal RAG search is a hop — no inference.rag_query row."""
    import app.api.ingestion.search as search_mod

    owner = make_user(db, username="csk_owner", role="developer")
    agent = make_agent(db, owner, name="csk-agent", approval_status="approved")
    coll = _make_collection(db, name="csk-coll", owner_id=owner.id)
    agent.bound_collection_id = coll.id
    db.commit()

    monkeypatch.setattr(
        search_mod, "_embed_query", AsyncMock(return_value=[0.0] * 8)
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: SimpleNamespace())

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        async def similarity_search_scoped_documents(self, **kwargs):
            return []

        async def similarity_search_per_document_authorized(self, **kwargs):
            return []

    monkeypatch.setattr(search_mod, "CollectionScopedPgVectorStore", _FakeStore)
    monkeypatch.setattr(
        search_mod,
        "_require_collection_clearance",
        lambda *a, **k: coll,
    )
    monkeypatch.setattr(
        search_mod,
        "_authorized_document_access",
        lambda *a, **k: {},
    )

    await search_mod.search_collection(
        collection_id=coll.id,
        payload=search_mod.SearchRequest(query="secret hop query", top_k=3),
        request=_make_request(),
        db=db,
        principal=search_mod.SearchPrincipal(user=owner, agent=agent),
        origin="csp",
    )
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert rows == []


# ── Admin API ────────────────────────────────────────────────────────────────


def _seed_inference_rows(db: Session) -> None:
    u1 = make_user(db, username="alice_audit")
    u2 = make_user(db, username="bob_audit")
    now = datetime.now(timezone.utc)
    rows = [
        AuditLog(
            actor_user_id=u1.id,
            actor_username="alice_audit",
            action="inference.chat",
            resource_type="inference",
            resource_id="m1",
            status="success",
            detail="alpha query about missiles",
            ip_address="198.51.100.1",
            created_at=now - timedelta(hours=2),
        ),
        AuditLog(
            actor_user_id=u2.id,
            actor_username="bob_audit",
            action="inference.rag_query",
            resource_type="inference",
            resource_id="9",
            status="success",
            detail="bravo search",
            ip_address="198.51.100.2",
            created_at=now - timedelta(hours=1),
        ),
        AuditLog(
            actor_user_id=u1.id,
            actor_username="alice_audit",
            action="inference.studio",
            resource_type="inference",
            resource_id="slides",
            status="success",
            detail="charlie deck",
            ip_address="198.51.100.1",
            created_at=now,
        ),
        AuditLog(
            actor_user_id=u1.id,
            actor_username="alice_audit",
            action="login",
            resource_type="auth",
            status="success",
            detail="not inference",
            ip_address="198.51.100.1",
            created_at=now,
        ),
    ]
    for r in rows:
        db.add(r)
    db.commit()


def test_admin_inference_filters_and_pagination(client: TestClient, db: Session):
    _seed_inference_rows(db)
    admin = make_user(db, username="inf_admin", role="admin")
    _grant_inference_audit_viewer(db, admin)

    resp = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"username": "alice_audit", "limit": 10, "offset": 0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert len(body["rows"]) == 2
    assert body["rows"][0]["detail"] == "charlie deck"  # newest first
    assert body["rows"][1]["detail"] == "alpha query about missiles"

    resp_ip = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"ip": "198.51.100.2"},
    )
    assert resp_ip.status_code == 422, resp_ip.text
    assert "IP" in resp_ip.json()["detail"]

    owner = make_user(db, username="inf_owner_ip", role="owner")
    resp_ip_owner = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(owner),
        params={"ip": "198.51.100.2"},
    )
    assert resp_ip_owner.status_code == 200, resp_ip_owner.text
    assert resp_ip_owner.json()["total"] == 1
    assert resp_ip_owner.json()["rows"][0]["actor_username"] == "bob_audit"

    resp_q = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"q": "missiles"},
    )
    assert resp_q.json()["total"] == 1

    resp_action = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"action": "inference.studio"},
    )
    assert resp_action.json()["total"] == 1

    now = datetime.now(timezone.utc)
    resp_date = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={
            "from": (now - timedelta(minutes=30)).isoformat(),
            "to": (now + timedelta(minutes=1)).isoformat(),
        },
    )
    assert resp_date.json()["total"] == 1
    assert resp_date.json()["rows"][0]["action"] == "inference.studio"

    resp_page = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"limit": 1, "offset": 1},
    )
    assert resp_page.json()["total"] == 3
    assert len(resp_page.json()["rows"]) == 1


def test_admin_inference_non_admin_403(client: TestClient, db: Session):
    user = make_user(db, username="plain_user")
    for path in (
        "/api/admin/audit/inference",
        "/api/admin/audit/inference/export",
    ):
        resp = client.get(path, headers=_bearer(user))
        assert resp.status_code == 403, path


def test_admin_inference_csv_export_bom_and_header(client: TestClient, db: Session):
    _seed_inference_rows(db)
    admin = make_user(db, username="csv_admin", role="admin")
    _grant_inference_audit_viewer(db, admin)
    resp = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(admin),
        params={"username": "alice_audit"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    raw = resp.content
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert "detail" in header
    assert "actor_username" in header
    rows = list(reader)
    assert len(rows) == 2


def test_audit_strict_fails_closed(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )
    user = make_user(db, username="strict_user")

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "app.services.inference_audit._persist_inference_audit_row", _boom
    )

    resp = client.post(
        "/api/tasks",
        headers=_bearer(user),
        json={
            "title": "strict studio prompt",
            "task_type": "generate_artifact",
            "requested_output_type": "report",
        },
    )
    assert resp.status_code == 503


def test_banners_uses_resolver(monkeypatch):
    from app.api import banners as banners_mod

    monkeypatch.setattr(
        "app.services.client_ip.settings.ANILA_TRUSTED_PROXY_CIDRS", ""
    )
    req = _make_request(peer="203.0.113.9", xff="1.2.3.4")
    assert banners_mod._client_ip(req) == "203.0.113.9"


# ── Denied / error outcomes + new entrances ───────────────────────────────────


def test_chat_model_permission_denied_writes_one_denied_row(
    client: TestClient, db: Session, monkeypatch
):
    """Authz denial after identity → status=denied, exactly one row, no success."""
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_denied_user", role="user")
    make_model(db, name="gpt-audit-denied")
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-audit-denied",
            "messages": [{"role": "user", "content": "should be denied"}],
            "stream": False,
        },
    )
    assert resp.status_code == 403, resp.text
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "inference.chat")
        .order_by(AuditLog.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "should be denied"
    assert rows[0].actor_username == "audit_denied_user"
    assert rows[0].resource_id == "gpt-audit-denied"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "model_permission_denied"
    success = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "inference.chat",
            AuditLog.status == "success",
        )
        .all()
    )
    assert success == []


def test_chat_upstream_error_writes_one_error_row(
    client: TestClient, db: Session, monkeypatch, _mock_upstream
):
    """Non-stream upstream failure → status=error, exactly one row (no success)."""
    from app.services import proxy_service

    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(proxy_service.settings, "PROXY_MAX_RETRIES", 1)
    monkeypatch.setattr(proxy_service.settings, "PROXY_RETRY_BASE_DELAY", 0)

    class _FailResponse:
        status_code = 500
        headers = {"content-type": "application/json"}
        text = '{"error":"boom"}'

        def json(self):
            return {"error": "boom"}

        def raise_for_status(self):
            raise RuntimeError("should not be called")

    class _FailClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _FailResponse()

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _FailClient(*a, **k)
    )

    user = make_user(db, username="audit_error_user", role="admin")
    make_model(db, name="gpt-audit-error")
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-audit-error",
            "messages": [{"role": "user", "content": "upstream will fail"}],
            "stream": False,
        },
    )
    assert resp.status_code in (500, 502), resp.text
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "inference.chat")
        .order_by(AuditLog.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].detail == "upstream will fail"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert "reason" in meta
    assert "success" not in {r.status for r in rows}


def _make_task(db: Session, user) -> object:
    from app.models.task import Task

    task = Task(
        title="audit image task",
        task_type="query",
        requester_user_id=user.id,
        status="submitted",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_image_generations_end_user_writes_inference_image(
    client: TestClient, db: Session, monkeypatch, _mock_upstream
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_image_user", role="admin")
    model = make_model(db, name="flux-audit")
    model.model_type = "image"
    model.is_image_primary = True
    db.commit()
    task = _make_task(db, user)

    resp = client.post(
        "/v1/images/generations",
        headers={**_bearer(user), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": model.name,
            "prompt": "draw a radar coverage map",
            "n": 1,
            "size": "1024x1024",
        },
    )
    assert resp.status_code == 200, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.image").all()
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].detail == "draw a radar coverage map"
    assert rows[0].resource_id == "flux-audit"
    assert rows[0].actor_username == "audit_image_user"


@pytest.mark.asyncio
async def test_image_generations_service_hop_skips_audit(db: Session, monkeypatch):
    from app.api import proxy as proxy_api
    from app.middleware.caller import Caller

    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_image_hop", role="admin")
    model = make_model(db, name="flux-hop")
    model.model_type = "image"
    model.is_image_primary = True
    db.commit()
    task = _make_task(db, user)

    body = {
        "model": model.name,
        "prompt": "service hop prompt must not audit",
        "n": 1,
    }
    body_bytes = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/images/generations",
        "raw_path": b"/v1/images/generations",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"x-anila-task-id", str(task.id).encode()),
        ],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    req = StarletteRequest(scope, receive)
    req.state.csp_caller = object()

    async def _fake_proxy_request(**kwargs):
        return {"created": 1, "data": [{"b64_json": "x"}]}

    monkeypatch.setattr(proxy_api, "proxy_request", _fake_proxy_request)
    monkeypatch.setattr(proxy_api, "begin_task_run", lambda *a, **k: SimpleNamespace(
        task_id=task.id,
        task_run_id=1,
        trace_id="tr-hop",
        owns_lifecycle=True,
        started_at=None,
    ))
    monkeypatch.setattr(
        proxy_api, "enforce_model_ceiling", lambda *a, **k: "無機密"
    )
    monkeypatch.setattr(
        proxy_api, "_verified_proxy_agent_context", lambda *a, **k: None
    )

    await proxy_api._image_generations_impl(
        req, caller=Caller(user=user, api_key_id=None), db=db
    )
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.image").all()
    assert rows == []


@pytest.mark.asyncio
async def test_session_answer_writes_inference_agent(db: Session, monkeypatch):
    import httpx
    from app.api import proxy as proxy_api
    from app.middleware.caller import Caller
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    monkeypatch.setattr(app_settings, "ANILA_PILOT_MODE", False)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    monkeypatch.setattr(
        "app.services.proxy_service._guard_outbound", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.services.proxy.service.lock_agent_registry_admission",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "app.services.proxy.service._commit_stream_admission",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.proxy_service.build_agent_headers",
        lambda *a, **k: {},
    )

    user = make_user(db, username="audit_session_user")
    owner = make_user(db, username="audit_session_owner", role="developer")
    agent = make_agent(
        db, owner, name="audit-session-agent", approval_status="approved"
    )
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    body = {
        "interrupt_id": "intr-1",
        "answer": "continue with the next step",
    }
    body_bytes = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/v1/agents/{agent.name}/sessions/sess-42/answer",
        "raw_path": b"/v1/agents/audit-session-agent/sessions/sess-42/answer",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    req = StarletteRequest(scope, receive)

    class _StreamCtx:
        status_code = 200

        async def aread(self):
            return b""

        async def aiter_lines(self):
            if False:
                yield ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **k):
            return _StreamCtx()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    resp = await proxy_api.resume_agent_session(
        agent.name,
        "sess-42",
        request=req,
        caller=Caller(user=user, api_key_id=None),
        db=db,
    )
    # Drain stream so generator runs
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk)

    rows = db.query(AuditLog).filter(AuditLog.action == "inference.agent").all()
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].detail == "continue with the next step"
    assert rows[0].resource_id == "audit-session-agent"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("session_id") == "sess-42"


@pytest.mark.asyncio
async def test_session_answer_service_hop_skips_audit(db: Session, monkeypatch):
    import httpx
    from app.api import proxy as proxy_api
    from app.middleware.caller import Caller
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    monkeypatch.setattr(app_settings, "ANILA_PILOT_MODE", False)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    monkeypatch.setattr(
        "app.services.proxy_service._guard_outbound", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "app.services.proxy.service.lock_agent_registry_admission",
        lambda **k: None,
    )
    monkeypatch.setattr(
        "app.services.proxy.service._commit_stream_admission",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.proxy_service.build_agent_headers",
        lambda *a, **k: {},
    )

    user = make_user(db, username="audit_session_hop")
    owner = make_user(db, username="audit_session_hop_owner", role="developer")
    agent = make_agent(
        db, owner, name="audit-session-hop-agent", approval_status="approved"
    )
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()

    body = {"interrupt_id": "intr-hop", "answer": "hop answer"}
    body_bytes = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/v1/agents/{agent.name}/sessions/sess-hop/answer",
        "raw_path": b"/answer",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    req = StarletteRequest(scope, receive)
    req.state.csp_caller = object()

    class _StreamCtx:
        status_code = 200

        async def aiter_lines(self):
            if False:
                yield ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **k):
            return _StreamCtx()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    await proxy_api.resume_agent_session(
        agent.name,
        "sess-hop",
        request=req,
        caller=Caller(user=user, api_key_id=None),
        db=db,
    )
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.agent").all()
    assert rows == []


# ── Revision 3: write-ahead / wide-CIDR / ILIKE escape ────────────────────────


def test_strict_nonstream_chat_write_ahead_before_upstream(
    client: TestClient, db: Session, monkeypatch
):
    """Strict mode commits acceptance row before upstream; upstream sees it."""
    from app.services import proxy_service

    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="strict_ahead_user", role="admin")
    make_model(db, name="gpt-strict-ahead")
    seen_upstream = {"called": False}

    class _PostResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {
                "id": "chatcmpl-strict",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        def raise_for_status(self):
            return None

    class _AssertClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            del json, headers  # unused; name shadows stdlib if kept
            seen_upstream["called"] = True
            rows = (
                db.query(AuditLog)
                .filter(AuditLog.action == "inference.chat")
                .all()
            )
            assert len(rows) == 1
            assert rows[0].status == "success"
            meta = __import__("json").loads(rows[0].metadata_json or "{}")
            assert meta.get("phase") == "acceptance"
            return _PostResponse()

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _AssertClient(*a, **k)
    )

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-strict-ahead",
            "messages": [{"role": "user", "content": "strict ahead prompt"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert seen_upstream["called"] is True
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.chat").all()
    assert len(rows) == 1
    assert json.loads(rows[0].metadata_json or "{}").get("phase") == "acceptance"


def test_strict_nonstream_chat_write_fail_skips_upstream(
    client: TestClient, db: Session, monkeypatch
):
    """Strict mode: audit write failure → 503 and upstream never runs."""
    from app.services import proxy_service

    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    user = make_user(db, username="strict_boom_user", role="admin")
    make_model(db, name="gpt-strict-boom")
    upstream_calls = {"n": 0}

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.inference_audit._persist_inference_audit_row", _boom)

    class _SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            upstream_calls["n"] += 1
            raise AssertionError("upstream must not be called")

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _SpyClient(*a, **k)
    )

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-strict-boom",
            "messages": [{"role": "user", "content": "must not reach model"}],
            "stream": False,
        },
    )
    assert resp.status_code == 503, resp.text
    assert upstream_calls["n"] == 0
    assert db.query(AuditLog).filter(AuditLog.action == "inference.chat").count() == 0


def test_strict_studio_503_leaves_no_task(client: TestClient, db: Session, monkeypatch):
    from app.models.task import Task

    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.inference_audit._persist_inference_audit_row", _boom)
    user = make_user(db, username="strict_studio_user")
    before = db.query(Task).count()
    resp = client.post(
        "/api/tasks",
        headers=_bearer(user),
        json={
            "title": "strict studio must not persist",
            "task_type": "generate_artifact",
            "requested_output_type": "report",
        },
    )
    assert resp.status_code == 503
    assert db.query(Task).count() == before


def test_wide_cidr_startup_warning(caplog, monkeypatch):
    import logging

    import app.services.client_ip as client_ip_mod

    client_ip_mod._trusted_cidrs_cache = None
    monkeypatch.setattr(
        "app.services.client_ip.settings.ANILA_TRUSTED_PROXY_CIDRS",
        "10.0.0.0/8",
    )
    with caplog.at_level(logging.WARNING, logger="app.services.client_ip"):
        resolve_client_ip(_make_request(peer="10.0.0.1", xff="198.51.100.7"))
    assert any(
        "過寬的信任代理網段會讓內網用戶端偽造 X-Forwarded-For" in r.message
        for r in caplog.records
    )


def test_admin_q_literal_percent_matches_only_literal(
    client: TestClient, db: Session
):
    admin = make_user(db, username="pct_admin", role="admin")
    _grant_inference_audit_viewer(db, admin)
    u = make_user(db, username="pct_actor")
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            AuditLog(
                actor_user_id=u.id,
                actor_username="pct_actor",
                action="inference.chat",
                resource_type="inference",
                resource_id="m",
                status="success",
                detail="progress 100% done",
                ip_address="198.51.100.9",
                created_at=now,
            ),
            AuditLog(
                actor_user_id=u.id,
                actor_username="pct_actor",
                action="inference.chat",
                resource_type="inference",
                resource_id="m",
                status="success",
                detail="progress 100X done",
                ip_address="198.51.100.9",
                created_at=now,
            ),
        ]
    )
    db.commit()
    resp = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"q": "%"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["rows"][0]["detail"] == "progress 100% done"




# ── Revision 4: pre-enrichment write-ahead / RAG outcome / CSV / RBAC ─────────


def test_strict_chat_acceptance_before_server_retrieval(
    client: TestClient, db: Session, monkeypatch
):
    """Strict mode: acceptance row exists before _prepare_server_retrieval runs."""
    from app.api import proxy as proxy_api
    from app.services import proxy_service

    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    user = make_user(db, username="strict_retrieval_order", role="admin")
    make_model(db, name="gpt-strict-retrieval-order")
    retrieval_seen = {"called": False}

    async def _spy_retrieval(*args, **kwargs):
        retrieval_seen["called"] = True
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "inference.chat")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "success"
        assert rows[0].detail == "before retrieval please"
        meta = json.loads(rows[0].metadata_json or "{}")
        assert meta.get("phase") == "acceptance"
        return None

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", _spy_retrieval)

    class _PostResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {
                "id": "chatcmpl-r4",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            del url, json, headers
            return _PostResponse()

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _Client(*a, **k)
    )

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-strict-retrieval-order",
            "messages": [{"role": "user", "content": "before retrieval please"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert retrieval_seen["called"] is True


def test_strict_chat_audit_fail_skips_server_retrieval(
    client: TestClient, db: Session, monkeypatch
):
    """Strict mode: failed acceptance write must not call retrieval."""
    from app.api import proxy as proxy_api
    from app.services import proxy_service

    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    user = make_user(db, username="strict_retrieval_boom", role="admin")
    make_model(db, name="gpt-strict-retrieval-boom")
    retrieval_calls = {"n": 0}

    async def _must_not_retrieve(*args, **kwargs):
        retrieval_calls["n"] += 1
        raise AssertionError("retrieval must not run after audit write failure")

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(proxy_api, "_prepare_server_retrieval", _must_not_retrieve)
    monkeypatch.setattr("app.services.inference_audit._persist_inference_audit_row", _boom)

    class _SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise AssertionError("upstream must not be called")

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _SpyClient(*a, **k)
    )

    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-strict-retrieval-boom",
            "messages": [{"role": "user", "content": "no retrieval"}],
            "stream": False,
        },
    )
    assert resp.status_code == 503, resp.text
    assert retrieval_calls["n"] == 0


def test_rag_embed_failure_writes_error_not_success(
    client: TestClient, db: Session, monkeypatch
):
    """Non-strict RAG: embed failure → one error row, no success row."""
    import app.api.ingestion.search as search_mod
    from fastapi import HTTPException

    user = make_user(db, username="rag_embed_fail_user")
    admin = make_user(db, username="rag_embed_fail_admin", role="admin")
    coll = _make_collection(db, name="rag-embed-fail-coll", owner_id=user.id)
    # Need ≥1 authorized document so search reaches embed (empty collections
    # short-circuit with success + results=[] before any embed call).
    db.add(
        IngestionDocument(
            collection_id=coll.id,
            filename="embed-fail.pdf",
            sha256="e" * 64,
            mime_type="application/pdf",
            status="indexed",
        )
    )
    now = datetime.now(timezone.utc)
    grant = issue_clearance_grant(
        db,
        actor=admin,
        subject_user_id=user.id,
        max_classification_level="絕對機密",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        basis_ticket="AUDIT-RAG-EMBED-FAIL",
    )
    grant_collection_access(
        db,
        actor=admin,
        clearance_grant_id=grant.id,
        collection_id=coll.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="AUDIT-NTK-EMBED-FAIL",
    )
    db.commit()

    async def _boom_embed(*args, **kwargs):
        raise HTTPException(status_code=503, detail="embed unavailable")

    monkeypatch.setattr(search_mod, "_embed_query", _boom_embed)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        headers=_bearer(user),
        json={"query": "explode the embedder", "top_k": 5},
    )
    assert resp.status_code == 503, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "error"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason")
    assert meta.get("phase") != "acceptance"


def test_csv_formula_injection_neutralized(client: TestClient, db: Session):
    admin = make_user(db, username="csv_formula_admin", role="admin")
    _grant_inference_audit_viewer(db, admin)
    actor = make_user(db, username="=CMD_actor")
    now = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            actor_user_id=actor.id,
            actor_username="=HYPERLINK",
            action="inference.chat",
            resource_type="inference",
            resource_id="+SUM(1,1)",
            status="success",
            detail="=1+1",
            ip_address="-1.2.3.4",
            metadata_json='@{"x":1}',
            created_at=now,
        )
    )
    db.commit()
    resp = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(admin),
        params={"username": "=HYPERLINK"},
    )
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    rows = list(reader)
    assert len(rows) == 1
    row = dict(zip(header, rows[0]))
    assert row["detail"].startswith("'")
    assert row["detail"] == "'=1+1"
    assert row["actor_username"] == "'=HYPERLINK"
    assert row["resource_id"] == "'+SUM(1,1)"
    # Non-owner admin: IP masked to sentinel (does not need formula prefix).
    assert row["ip_address"] == "<owner-only>"
    assert row["metadata_json"] == ""


def test_inference_audit_owner_sees_raw_ip_metadata_admin_masked(
    client: TestClient, db: Session
):
    owner = make_user(db, username="inf_owner_r4", role="owner")
    admin = make_user(db, username="inf_admin_r4", role="admin")
    _grant_inference_audit_viewer(db, admin)
    actor = make_user(db, username="inf_actor_r4")
    now = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            actor_user_id=actor.id,
            actor_username="inf_actor_r4",
            action="inference.chat",
            resource_type="inference",
            resource_id="m1",
            status="success",
            detail="full prompt remains visible",
            ip_address="198.51.100.77",
            metadata_json='{"model":"gpt","phase":"acceptance"}',
            created_at=now,
        )
    )
    db.commit()

    admin_list = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"username": "inf_actor_r4"},
    )
    assert admin_list.status_code == 200
    admin_row = admin_list.json()["rows"][0]
    assert admin_row["detail"] == "full prompt remains visible"
    assert admin_row["ip_address"] == "<owner-only>"
    assert admin_row["metadata_json"] is None

    owner_list = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(owner),
        params={"username": "inf_actor_r4"},
    )
    assert owner_list.status_code == 200
    owner_row = owner_list.json()["rows"][0]
    assert owner_row["ip_address"] == "198.51.100.77"
    assert owner_row["metadata_json"] == '{"model":"gpt","phase":"acceptance"}'

    admin_csv = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(admin),
        params={"username": "inf_actor_r4"},
    )
    assert admin_csv.status_code == 200
    admin_csv_rows = list(csv.DictReader(io.StringIO(admin_csv.content.decode("utf-8-sig"))))
    assert admin_csv_rows[0]["ip_address"] == "<owner-only>"
    assert admin_csv_rows[0]["metadata_json"] == ""
    assert admin_csv_rows[0]["detail"] == "full prompt remains visible"

    owner_csv = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(owner),
        params={"username": "inf_actor_r4"},
    )
    assert owner_csv.status_code == 200
    owner_csv_rows = list(csv.DictReader(io.StringIO(owner_csv.content.decode("utf-8-sig"))))
    assert owner_csv_rows[0]["ip_address"] == "198.51.100.77"
    assert owner_csv_rows[0]["metadata_json"] == '{"model":"gpt","phase":"acceptance"}'


# ── Revision 5: per-admin inference-audit viewer grant ───────────────────────


def test_r1_0033_migration_chains_after_r1_0032():
    from pathlib import Path

    mig = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0033_user_can_view_inference_audit.py"
    )
    text = mig.read_text(encoding="utf-8")
    assert 'revision: str = "r1_0033"' in text
    assert 'down_revision: Union[str, None] = "r1_0032"' in text
    assert "can_view_inference_audit" in text


def test_non_granted_admin_forbidden_on_list_and_export(
    client: TestClient, db: Session
):
    admin = make_user(db, username="inf_admin_nogrant", role="admin")
    assert admin.can_view_inference_audit is False
    for path in (
        "/api/admin/audit/inference",
        "/api/admin/audit/inference/export",
    ):
        resp = client.get(path, headers=_bearer(admin))
        assert resp.status_code == 403, path


def test_granted_admin_list_export_masked_owner_raw(
    client: TestClient, db: Session
):
    owner = make_user(db, username="inf_owner_r5", role="owner")
    admin = make_user(db, username="inf_admin_r5", role="admin")
    _grant_inference_audit_viewer(db, admin)
    actor = make_user(db, username="inf_actor_r5")
    now = datetime.now(timezone.utc)
    db.add(
        AuditLog(
            actor_user_id=actor.id,
            actor_username="inf_actor_r5",
            action="inference.chat",
            resource_type="inference",
            resource_id="m1",
            status="success",
            detail="prompt text",
            ip_address="203.0.113.50",
            metadata_json='{"phase":"acceptance"}',
            created_at=now,
        )
    )
    db.commit()

    admin_list = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"username": "inf_actor_r5"},
    )
    assert admin_list.status_code == 200
    admin_row = admin_list.json()["rows"][0]
    assert admin_row["ip_address"] == "<owner-only>"
    assert admin_row["metadata_json"] is None
    assert admin_row["detail"] == "prompt text"

    owner_list = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(owner),
        params={"username": "inf_actor_r5"},
    )
    assert owner_list.status_code == 200
    owner_row = owner_list.json()["rows"][0]
    assert owner_row["ip_address"] == "203.0.113.50"
    assert owner_row["metadata_json"] == '{"phase":"acceptance"}'

    admin_csv = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(admin),
        params={"username": "inf_actor_r5"},
    )
    assert admin_csv.status_code == 200
    admin_csv_row = list(
        csv.DictReader(io.StringIO(admin_csv.content.decode("utf-8-sig")))
    )[0]
    assert admin_csv_row["ip_address"] == "<owner-only>"
    assert admin_csv_row["metadata_json"] == ""


def test_owner_only_toggle_writes_audit_viewer_grant(
    client: TestClient, db: Session
):
    owner = make_user(db, username="grant_owner_r5", role="owner")
    admin = make_user(db, username="grant_admin_r5", role="admin")
    target = make_user(db, username="grant_target_r5", role="admin")

    denied = client.put(
        f"/api/users/{target.id}",
        headers=_bearer(admin),
        json={"can_view_inference_audit": True},
    )
    assert denied.status_code == 403
    db.refresh(target)
    assert target.can_view_inference_audit is False

    missing = client.put(
        "/api/users/999999",
        headers=_bearer(owner),
        json={"can_view_inference_audit": True},
    )
    assert missing.status_code == 404

    enabled = client.put(
        f"/api/users/{target.id}",
        headers=_bearer(owner),
        json={"can_view_inference_audit": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["can_view_inference_audit"] is True
    db.refresh(target)
    assert target.can_view_inference_audit is True

    grant_rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "audit.viewer_grant",
            AuditLog.resource_type == "user",
            AuditLog.resource_id == str(target.id),
        )
        .order_by(AuditLog.id.asc())
        .all()
    )
    assert len(grant_rows) == 1
    assert grant_rows[0].detail == "enabled"
    assert grant_rows[0].actor_user_id == owner.id
    assert grant_rows[0].actor_username == owner.username

    disabled = client.put(
        f"/api/users/{target.id}",
        headers=_bearer(owner),
        json={"can_view_inference_audit": False},
    )
    assert disabled.status_code == 200
    grant_rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "audit.viewer_grant",
            AuditLog.resource_id == str(target.id),
        )
        .order_by(AuditLog.id.asc())
        .all()
    )
    assert len(grant_rows) == 2
    assert grant_rows[1].detail == "disabled"

    # Idempotent re-toggle of same value must not write another grant row.
    again = client.put(
        f"/api/users/{target.id}",
        headers=_bearer(owner),
        json={"can_view_inference_audit": False},
    )
    assert again.status_code == 200
    assert (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "audit.viewer_grant",
            AuditLog.resource_id == str(target.id),
        )
        .count()
        == 2
    )


def test_non_owner_ip_filter_rejected_on_list_and_export(
    client: TestClient, db: Session
):
    """Granted admin must not IP-oracle; owner keeps the filter."""
    _seed_inference_rows(db)
    admin = make_user(db, username="ip_oracle_admin", role="admin")
    _grant_inference_audit_viewer(db, admin)
    owner = make_user(db, username="ip_oracle_owner", role="owner")

    for path in (
        "/api/admin/audit/inference",
        "/api/admin/audit/inference/export",
    ):
        denied = client.get(
            path,
            headers=_bearer(admin),
            params={"ip": "198.51.100.1"},
        )
        assert denied.status_code == 422, path
        assert "IP" in denied.json()["detail"]

    allowed = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(owner),
        params={"ip": "198.51.100.1"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["total"] == 2
    assert all(r["ip_address"] == "198.51.100.1" for r in allowed.json()["rows"])


def test_rag_expand_relations_failure_writes_error_not_success(
    client: TestClient, db: Session, monkeypatch
):
    """expand_relations failure is inside the retrieval error envelope."""
    import app.api.ingestion.search as search_mod

    user = make_user(db, username="rag_expand_fail_user")
    admin = make_user(db, username="rag_expand_fail_admin", role="admin")
    coll = _make_collection(db, name="rag-expand-fail-coll", owner_id=user.id)
    doc = IngestionDocument(
        collection_id=coll.id,
        filename="expand-fail.pdf",
        sha256="f" * 64,
        mime_type="application/pdf",
        status="indexed",
    )
    db.add(doc)
    now = datetime.now(timezone.utc)
    grant = issue_clearance_grant(
        db,
        actor=admin,
        subject_user_id=user.id,
        max_classification_level="絕對機密",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        basis_ticket="AUDIT-RAG-EXPAND-FAIL",
    )
    grant_collection_access(
        db,
        actor=admin,
        clearance_grant_id=grant.id,
        collection_id=coll.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="AUDIT-NTK-EXPAND-FAIL",
    )
    db.commit()
    db.refresh(doc)

    monkeypatch.setattr(
        search_mod, "_embed_query", AsyncMock(return_value=[0.1] * 8)
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: SimpleNamespace())

    fake_chunk = SimpleNamespace(
        id=1,
        document_id=doc.id,
        chunk_key="c1",
        content="hit content",
        metadata={},
        parent_chunk_id=None,
        chunk_type="leaf",
        chunk_level=0,
    )
    fake_hit = SimpleNamespace(chunk=fake_chunk, score=0.9, parent_content=None)

    class _FakeStore:
        def __init__(self, *a, **k):
            pass

        async def similarity_search_scoped_documents(self, **kwargs):
            return [fake_hit]

        async def similarity_search_per_document_authorized(self, **kwargs):
            return [fake_hit]

    async def _boom_expand(*args, **kwargs):
        raise RuntimeError("relation expand blew up")

    monkeypatch.setattr(search_mod, "CollectionScopedPgVectorStore", _FakeStore)
    monkeypatch.setattr(search_mod, "_expand_relations", _boom_expand)

    with pytest.raises(RuntimeError, match="relation expand blew up"):
        client.post(
            f"/api/ingestion/collections/{coll.id}/search",
            headers=_bearer(user),
            json={
                "query": "expand me",
                "top_k": 5,
                "expand_relations": True,
            },
        )
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert not any(r.status == "success" for r in rows)


def test_csv_export_batches_and_caps_without_date(
    client: TestClient, db: Session, monkeypatch
):
    """Unscoped export streams in batches and hard-caps with a truncation marker."""
    import app.api.admin_inference_audit as audit_api

    monkeypatch.setattr(audit_api, "EXPORT_BATCH_SIZE", 2)
    monkeypatch.setattr(audit_api, "EXPORT_MAX_ROWS_WITHOUT_DATE", 5)

    owner = make_user(db, username="csv_cap_owner", role="owner")
    actor = make_user(db, username="csv_cap_actor")
    now = datetime.now(timezone.utc)
    for i in range(8):
        db.add(
            AuditLog(
                actor_user_id=actor.id,
                actor_username="csv_cap_actor",
                action="inference.chat",
                resource_type="inference",
                resource_id=f"m{i}",
                status="success",
                detail=f"cap row {i}",
                ip_address="198.51.100.10",
                created_at=now - timedelta(seconds=i),
            )
        )
    db.commit()

    batch_limits: list[int] = []
    original_build = audit_api._build_inference_query

    class _SpyQuery:
        def __init__(self, q):
            self._q = q

        def filter(self, *args, **kwargs):
            return _SpyQuery(self._q.filter(*args, **kwargs))

        def limit(self, n):
            batch_limits.append(n)
            return _SpyQuery(self._q.limit(n))

        def all(self):
            return self._q.all()

        def __getattr__(self, name):
            return getattr(self._q, name)

    def _spy_build(*args, **kwargs):
        return _SpyQuery(original_build(*args, **kwargs))

    monkeypatch.setattr(audit_api, "_build_inference_query", _spy_build)

    resp = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(owner),
    )
    assert resp.status_code == 200, resp.text
    text = resp.content.decode("utf-8-sig")
    assert "# truncated at 5 — 請縮小日期範圍" in text
    reader = csv.reader(io.StringIO(text.split("# truncated")[0]))
    header = next(reader)
    data_rows = [r for r in reader if r]
    assert len(data_rows) == 5
    assert header[0] == "id"
    # Batch size 2 → multiple limit(2) fetches before hitting the cap.
    assert batch_limits.count(2) >= 2

    # Date-bounded export is not hard-capped at 5.
    resp_dated = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(owner),
        params={
            "from": (now - timedelta(hours=1)).isoformat(),
            "to": (now + timedelta(minutes=1)).isoformat(),
        },
    )
    assert resp_dated.status_code == 200
    dated_text = resp_dated.content.decode("utf-8-sig")
    assert "# truncated" not in dated_text
    dated_reader = csv.reader(io.StringIO(dated_text))
    next(dated_reader)
    dated_rows = [r for r in dated_reader if r]
    assert len(dated_rows) == 8


# ── Revision 7: task-run vs strict / inactive collection / embed / UTC ────────


def test_strict_chat_audit_fail_with_task_id_leaves_no_running_taskrun(
    client: TestClient, db: Session, monkeypatch
):
    """Strict + X-ANILA-Task-Id: audit write failure must 503 with no running TaskRun."""
    from app.models.task import TaskRun
    from app.services import proxy_service

    monkeypatch.setattr(
        "app.services.inference_audit.settings.ANILA_AUDIT_STRICT", True
    )
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    user = make_user(db, username="strict_task_zombie", role="admin")
    make_model(db, name="gpt-strict-task-zombie")
    task = _make_task(db, user)

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.inference_audit._persist_inference_audit_row", _boom)

    class _SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise AssertionError("upstream must not be called")

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _SpyClient(*a, **k)
    )

    before_runs = db.query(TaskRun).filter(TaskRun.status == "running").count()
    resp = client.post(
        "/v1/chat/completions",
        headers={**_bearer(user), "X-ANILA-Task-Id": str(task.id)},
        json={
            "model": "gpt-strict-task-zombie",
            "messages": [{"role": "user", "content": "no zombie please"}],
            "stream": False,
        },
    )
    assert resp.status_code == 503, resp.text
    db.expire_all()
    running = (
        db.query(TaskRun)
        .filter(TaskRun.task_id == task.id, TaskRun.status == "running")
        .count()
    )
    assert running == 0
    assert db.query(TaskRun).filter(TaskRun.status == "running").count() == before_runs


def _grant_rag_access(db: Session, *, user, admin, coll) -> None:
    now = datetime.now(timezone.utc)
    grant = issue_clearance_grant(
        db,
        actor=admin,
        subject_user_id=user.id,
        max_classification_level="絕對機密",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        basis_ticket="AUDIT-INACTIVE",
    )
    grant_collection_access(
        db,
        actor=admin,
        clearance_grant_id=grant.id,
        collection_id=coll.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="AUDIT-NTK-INACTIVE",
    )
    db.commit()


def test_inactive_collection_text_search_writes_denied_row(
    client: TestClient, db: Session
):
    user = make_user(db, username="inactive_text_user")
    admin = make_user(db, username="inactive_text_admin", role="admin")
    coll = _make_collection(db, name="inactive-text-coll", owner_id=user.id)
    coll.status = "disabled"
    db.commit()
    _grant_rag_access(db, user=user, admin=admin, coll=coll)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        headers=_bearer(user),
        json={"query": "should be denied for inactive", "top_k": 3},
    )
    assert resp.status_code == 409, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "should be denied for inactive"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "collection_inactive"


def test_inactive_collection_image_search_writes_denied_row(
    client: TestClient, db: Session
):
    user = make_user(db, username="inactive_img_user")
    admin = make_user(db, username="inactive_img_admin", role="admin")
    coll = _make_collection(db, name="inactive-img-coll", owner_id=user.id)
    coll.status = "archived"
    db.commit()
    _grant_rag_access(db, user=user, admin=admin, coll=coll)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/images/search",
        headers=_bearer(user),
        json={"query": "inactive image search", "top_k": 3},
    )
    assert resp.status_code == 409, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "inactive image search"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "collection_inactive"
    assert meta.get("image_search") is True


def test_embeddings_v1_end_user_writes_inference_embed(
    client: TestClient, db: Session, monkeypatch, _mock_upstream
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_embed_v1", role="admin")
    make_model(db, name="embed-audit-v1")
    resp = client.post(
        "/v1/embeddings",
        headers=_bearer(user),
        json={"model": "embed-audit-v1", "input": "vector me please"},
    )
    assert resp.status_code == 200, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.embed").all()
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].detail == "vector me please"
    assert rows[0].resource_id == "embed-audit-v1"
    assert rows[0].actor_username == "audit_embed_v1"


def test_embeddings_v2_end_user_writes_list_input_joined(
    client: TestClient, db: Session, monkeypatch, _mock_upstream
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_embed_v2", role="admin")
    make_model(db, name="embed-audit-v2")
    resp = client.post(
        "/v2/embeddings",
        headers=_bearer(user),
        json={"model": "embed-audit-v2", "input": ["line one", "line two"]},
    )
    assert resp.status_code == 200, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.embed").all()
    assert len(rows) == 1
    assert rows[0].detail == "line one\nline two"
    assert rows[0].resource_id == "embed-audit-v2"


@pytest.mark.asyncio
async def test_embeddings_service_hop_skips_audit(db: Session, monkeypatch):
    from app.api import proxy as proxy_api
    from app.middleware.caller import Caller

    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="audit_embed_hop", role="admin")
    make_model(db, name="embed-hop")

    body = {"model": "embed-hop", "input": "hop must not audit"}
    body_bytes = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/embeddings",
        "raw_path": b"/v1/embeddings",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("203.0.113.10", 12345),
        "server": ("testserver", 80),
    }
    req = StarletteRequest(scope, receive)
    req.state.csp_caller = object()

    async def _fake_proxy_request(**kwargs):
        return {"object": "list", "data": []}

    monkeypatch.setattr(proxy_api, "proxy_request", _fake_proxy_request)
    monkeypatch.setattr(
        proxy_api, "_verified_proxy_agent_context", lambda *a, **k: None
    )

    await proxy_api.embeddings_v1(
        req, caller=Caller(user=user, api_key_id=None), db=db
    )
    assert db.query(AuditLog).filter(AuditLog.action == "inference.embed").count() == 0


def test_embeddings_denied_writes_denied_row(
    client: TestClient, db: Session, monkeypatch
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    denied = make_user(db, username="embed_denied_user", role="user")
    make_model(db, name="embed-denied-model")

    resp = client.post(
        "/v1/embeddings",
        headers=_bearer(denied),
        json={"model": "embed-denied-model", "input": "no access"},
    )
    assert resp.status_code == 403, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.embed").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "no access"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "model_permission_denied"


def test_created_at_serialized_as_utc_aware(client: TestClient, db: Session):
    from app.api.admin_inference_audit import _serialize_created_at

    naive = datetime(2026, 7, 23, 4, 0, 0)
    out = _serialize_created_at(naive)
    assert out is not None
    assert out.endswith("+00:00") or out.endswith("Z")
    assert out.startswith("2026-07-23T04:00:00")

    admin = make_user(db, username="tz_admin", role="admin")
    _grant_inference_audit_viewer(db, admin)
    u = make_user(db, username="tz_actor")
    db.add(
        AuditLog(
            actor_user_id=u.id,
            actor_username="tz_actor",
            action="inference.chat",
            resource_type="inference",
            resource_id="m",
            status="success",
            detail="tz check",
            ip_address="198.51.100.9",
            created_at=naive,
        )
    )
    db.commit()
    resp = client.get(
        "/api/admin/audit/inference",
        headers=_bearer(admin),
        params={"username": "tz_actor"},
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()["rows"][0]["created_at"]
    assert created.endswith("+00:00") or created.endswith("Z")

    resp_csv = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(admin),
        params={"username": "tz_actor"},
    )
    assert resp_csv.status_code == 200
    text = resp_csv.content.decode("utf-8-sig")
    assert "2026-07-23T04:00:00+00:00" in text or "2026-07-23T04:00:00Z" in text


# ── Revision 9: failure-log redaction / model 404 / embed 403 / keyset export ─


def test_audit_write_failure_logs_omit_raw_prompt(db: Session, monkeypatch, caplog):
    """On audit-write failure, logs must not contain the raw prompt detail."""
    import hashlib
    import logging

    from app.services import inference_audit as ia

    secret = "SUPER_SECRET_PROMPT_TOKEN_do_not_log_me"
    detail_len = len(secret.encode("utf-8"))
    detail_sha = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]
    user = make_user(db, username="fail_log_user")

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(ia, "_persist_inference_audit_row", _boom)
    monkeypatch.setattr(ia.settings, "ANILA_AUDIT_STRICT", False)

    with caplog.at_level(logging.ERROR):
        ia.record_inference_audit(
            db,
            request=_make_request(peer="198.51.100.77"),
            actor=user,
            action="inference.chat",
            resource_id="gpt-x",
            detail=secret,
            status="success",
            commit=True,
        )

    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
        if record.args:
            joined = " ".join(str(a) for a in record.args)
            assert secret not in joined
    assert any("detail_len" in r.getMessage() for r in caplog.records)
    assert any(str(detail_len) in (r.getMessage() + str(r.args)) for r in caplog.records)
    assert any(detail_sha in (r.getMessage() + str(r.args)) for r in caplog.records)
    assert any(str(user.id) in str(r.args) for r in caplog.records if r.args)

    # Strict path: still 503, still no raw prompt in logs.
    monkeypatch.setattr(ia.settings, "ANILA_AUDIT_STRICT", True)
    caplog.clear()
    from fastapi import HTTPException

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_info:
            ia.record_inference_audit(
                db,
                request=_make_request(peer="198.51.100.77"),
                actor=user,
                action="inference.chat",
                resource_id="gpt-x",
                detail=secret,
                status="success",
                commit=True,
            )
    assert exc_info.value.status_code == 503
    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
        if record.args:
            assert secret not in " ".join(str(a) for a in record.args)
    assert any(detail_sha in (r.getMessage() + str(r.args)) for r in caplog.records)


def test_chat_unknown_model_writes_denied_row(
    client: TestClient, db: Session, monkeypatch
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="unknown_model_user", role="admin")
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "totally-unregistered-model",
            "messages": [{"role": "user", "content": "where is my model"}],
            "stream": False,
        },
    )
    assert resp.status_code == 404, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.chat").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "where is my model"
    assert rows[0].resource_id == "totally-unregistered-model"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "model_not_found"


def test_chat_disabled_model_writes_denied_row(
    client: TestClient, db: Session, monkeypatch
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="disabled_model_user", role="admin")
    model = make_model(db, name="gpt-disabled-audit")
    model.is_active = False
    db.commit()
    resp = client.post(
        "/v1/chat/completions",
        headers=_bearer(user),
        json={
            "model": "gpt-disabled-audit",
            "messages": [{"role": "user", "content": "disabled please"}],
            "stream": False,
        },
    )
    assert resp.status_code == 400, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.chat").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "disabled please"
    assert rows[0].resource_id == "gpt-disabled-audit"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "model_disabled"


def test_embeddings_unknown_model_writes_denied_row(
    client: TestClient, db: Session, monkeypatch
):
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    user = make_user(db, username="embed_unknown_user", role="admin")
    resp = client.post(
        "/v1/embeddings",
        headers=_bearer(user),
        json={"model": "no-such-embed-model", "input": "embed unknown"},
    )
    assert resp.status_code == 404, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.embed").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].detail == "embed unknown"
    assert rows[0].resource_id == "no-such-embed-model"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "model_not_found"


def _rag_coll_with_doc(db: Session, *, user, admin, name: str):
    coll = _make_collection(db, name=name, owner_id=user.id)
    db.add(
        IngestionDocument(
            collection_id=coll.id,
            filename=f"{name}.pdf",
            sha256=("a" * 64),
            mime_type="application/pdf",
            status="indexed",
        )
    )
    now = datetime.now(timezone.utc)
    grant = issue_clearance_grant(
        db,
        actor=admin,
        subject_user_id=user.id,
        max_classification_level="絕對機密",
        valid_from=now - timedelta(minutes=5),
        expires_at=now + timedelta(hours=1),
        basis_ticket=f"AUDIT-{name}",
    )
    grant_collection_access(
        db,
        actor=admin,
        clearance_grant_id=grant.id,
        collection_id=coll.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket=f"AUDIT-NTK-{name}",
    )
    db.commit()
    return coll


def test_rag_embed_403_writes_denied_not_error(
    client: TestClient, db: Session, monkeypatch
):
    import app.api.ingestion.search as search_mod
    from fastapi import HTTPException

    user = make_user(db, username="rag_embed_403_user")
    admin = make_user(db, username="rag_embed_403_admin", role="admin")
    coll = _rag_coll_with_doc(db, user=user, admin=admin, name="rag-embed-403")

    async def _deny_embed(*args, **kwargs):
        raise HTTPException(status_code=403, detail="embedding_policy_denied: nope")

    monkeypatch.setattr(search_mod, "_embed_query", _deny_embed)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        headers=_bearer(user),
        json={"query": "policy blocked query", "top_k": 3},
    )
    assert resp.status_code == 403, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "embedding_policy_denied"


def test_rag_embed_500_still_writes_error(
    client: TestClient, db: Session, monkeypatch
):
    import app.api.ingestion.search as search_mod
    from fastapi import HTTPException

    user = make_user(db, username="rag_embed_500_user")
    admin = make_user(db, username="rag_embed_500_admin", role="admin")
    coll = _rag_coll_with_doc(db, user=user, admin=admin, name="rag-embed-500")

    async def _boom_embed(*args, **kwargs):
        raise HTTPException(status_code=500, detail="embed crashed")

    monkeypatch.setattr(search_mod, "_embed_query", _boom_embed)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        headers=_bearer(user),
        json={"query": "server error embed", "top_k": 3},
    )
    assert resp.status_code == 500, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "error"


def test_rag_image_embed_403_writes_denied(
    client: TestClient, db: Session, monkeypatch
):
    import app.api.ingestion.search as search_mod
    from fastapi import HTTPException

    user = make_user(db, username="rag_img_403_user")
    admin = make_user(db, username="rag_img_403_admin", role="admin")
    coll = _rag_coll_with_doc(db, user=user, admin=admin, name="rag-img-403")

    async def _deny_embed(*args, **kwargs):
        raise HTTPException(status_code=403, detail="embedding_policy_denied: img")

    monkeypatch.setattr(search_mod, "_embed_query", _deny_embed)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/images/search",
        headers=_bearer(user),
        json={"query": "image policy deny", "top_k": 3},
    )
    assert resp.status_code == 403, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "denied"
    meta = json.loads(rows[0].metadata_json or "{}")
    assert meta.get("reason") == "embedding_policy_denied"


def test_rag_image_embed_500_writes_error(
    client: TestClient, db: Session, monkeypatch
):
    import app.api.ingestion.search as search_mod
    from fastapi import HTTPException

    user = make_user(db, username="rag_img_500_user")
    admin = make_user(db, username="rag_img_500_admin", role="admin")
    coll = _rag_coll_with_doc(db, user=user, admin=admin, name="rag-img-500")

    async def _boom_embed(*args, **kwargs):
        raise HTTPException(status_code=500, detail="img embed crashed")

    monkeypatch.setattr(search_mod, "_embed_query", _boom_embed)

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/images/search",
        headers=_bearer(user),
        json={"query": "image server error", "top_k": 3},
    )
    assert resp.status_code == 500, resp.text
    rows = db.query(AuditLog).filter(AuditLog.action == "inference.rag_query").all()
    assert len(rows) == 1
    assert rows[0].status == "error"


def test_csv_export_keyset_stable_under_concurrent_inserts(
    client: TestClient, db: Session, monkeypatch
):
    """Rows inserted between keyset batches must not duplicate/skip pre-existing."""
    import app.api.admin_inference_audit as audit_api

    monkeypatch.setattr(audit_api, "EXPORT_BATCH_SIZE", 2)
    monkeypatch.setattr(audit_api, "EXPORT_MAX_ROWS_WITHOUT_DATE", 50_000)

    owner = make_user(db, username="keyset_owner", role="owner")
    actor = make_user(db, username="keyset_actor")
    now = datetime.now(timezone.utc)
    preexisting_ids: list[int] = []
    for i in range(6):
        row = AuditLog(
            actor_user_id=actor.id,
            actor_username="keyset_actor",
            action="inference.chat",
            resource_type="inference",
            resource_id=f"ks{i}",
            status="success",
            detail=f"keyset row {i}",
            ip_address="198.51.100.20",
            created_at=now - timedelta(seconds=i),
        )
        db.add(row)
        db.flush()
        preexisting_ids.append(row.id)
    db.commit()

    original_build = audit_api._build_inference_query
    batches_seen = {"n": 0}

    class _SpyQuery:
        def __init__(self, q):
            self._q = q

        def filter(self, *args, **kwargs):
            return _SpyQuery(self._q.filter(*args, **kwargs))

        def limit(self, n):
            return _SpyQuery(self._q.limit(n))

        def all(self):
            rows = self._q.all()
            batches_seen["n"] += 1
            if batches_seen["n"] == 1 and rows:
                # Newer row arrives after first keyset page — must not shift pages.
                db.add(
                    AuditLog(
                        actor_user_id=actor.id,
                        actor_username="keyset_actor",
                        action="inference.chat",
                        resource_type="inference",
                        resource_id="ks-concurrent",
                        status="success",
                        detail="inserted mid-export",
                        ip_address="198.51.100.20",
                        created_at=now + timedelta(seconds=5),
                    )
                )
                db.commit()
            return rows

        def __getattr__(self, name):
            return getattr(self._q, name)

    monkeypatch.setattr(
        audit_api,
        "_build_inference_query",
        lambda *a, **k: _SpyQuery(original_build(*a, **k)),
    )

    resp = client.get(
        "/api/admin/audit/inference/export",
        headers=_bearer(owner),
        params={"username": "keyset_actor"},
    )
    assert resp.status_code == 200, resp.text
    text = resp.content.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    data_rows = [r for r in reader if r and not r[0].startswith("#")]
    id_idx = header.index("id")
    exported_ids = [int(r[id_idx]) for r in data_rows]

    # Pre-existing rows: exactly once each, none skipped.
    for pid in preexisting_ids:
        assert exported_ids.count(pid) == 1, (pid, exported_ids)
    assert sorted(exported_ids) == sorted(preexisting_ids) or set(
        preexisting_ids
    ).issubset(set(exported_ids))
    # Stronger: no duplicates at all among exported ids.
    assert len(exported_ids) == len(set(exported_ids))
    # Pre-existing set fully present (concurrent insert may or may not appear).
    assert set(preexisting_ids).issubset(set(exported_ids))
    assert batches_seen["n"] >= 3
