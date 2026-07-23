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
from app.models.ingestion import IngestionCollection
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
    assert resp_ip.json()["total"] == 1
    assert resp_ip.json()["rows"][0]["actor_username"] == "bob_audit"

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
        "app.services.inference_audit.log_audit_event", _boom
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

    monkeypatch.setattr("app.services.inference_audit.log_audit_event", _boom)

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

    monkeypatch.setattr("app.services.inference_audit.log_audit_event", _boom)
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

