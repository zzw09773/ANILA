"""Slice 4a — Full Trace foundation: ingest + query endpoints + proxy spans.

Locks the frozen wire contract:

- ``POST /v1/traces/{trace_id}/spans`` — data-plane batch ingest (1..256),
  idempotent per (trace_id, span_id), 413 oversized / 422 invalid / 401
  anonymous, task_id linked via tasks.trace_id, producer from caller type.
- ``GET /api/traces/{trace_id}`` — admin/owner OR task requester; flat span
  list sorted by started_at then span_id; 404 when neither spans nor task.
- Proxy self-emitted spans: a task-linked chat produces a proxy.dispatch
  parent (``agent.run.finished``) + model/agent child span with correct
  parentage and linked task_id; legacy (no-task) calls produce NO spans;
  an upstream error still records spans with error status.
"""

from __future__ import annotations

import os

# Same house pattern as test_proxy_task_wiring.py: startup_security blocks dev
# default secrets in production mode — allow in tests.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from anila_core.tracing import TraceExporter

from app.models.service_client import ServiceClient
from app.models.clearance import ClearanceGrant
from app.models.trace_span import TraceSpan
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from app.services import proxy_service
from app.services.agent_registry import build_registry_snapshot
from app.services.proxy import service as proxy_impl
from app.services.proxy import spans as proxy_spans
from app.services.proxy import task_link

from tests.conftest import (
    login,
    make_model,
    make_user,
)
from tests.test_proxy_task_wiring import (
    _bearer,
    _jwt,
    _make_task,
    _patch_post_client,
    _patch_stream_client,
)
from tests.test_gate5_r2_agent_readiness import _ready_agent


# ── Ingest / query fixtures ────────────────────────────────────────────────


def _seed_span(
    db: Session,
    trace_id: str,
    span_id: str,
    *,
    task_id: int | None = None,
    started_at: datetime | None = None,
    span_type: str = "agent.step.started",
    producer: str = "proxy",
) -> TraceSpan:
    row = TraceSpan(
        trace_id=trace_id,
        span_id=span_id,
        task_id=task_id,
        span_type=span_type,
        name=span_id,
        started_at=started_at,
        status="ok",
        producer=producer,
    )
    db.add(row)
    db.commit()
    return row


def _grant_trace_read_clearance(
    db: Session,
    *,
    subject,
    issuer,
    level: str = "無機密",
) -> None:
    now = datetime.now(timezone.utc)
    db.add(
        ClearanceGrant(
            subject_user_id=subject.id,
            max_classification_level=level,
            valid_from=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            basis_ticket=f"trace-read-{subject.username}",
            issued_by_user_id=issuer.id,
        )
    )
    db.commit()


def _span_payload(span_id: str, **over) -> dict:
    body = {
        "span_id": span_id,
        "span_type": "agent.run.started",
        "name": "run",
    }
    body.update(over)
    return body


class TestIngest:
    def test_core_exporter_posts_with_live_csp_bearer_contract(
        self, client: TestClient, db: Session, monkeypatch
    ):
        """Exercise the real anila-core sender against CSP, not a mock URL."""
        monkeypatch.setenv("SECRET_KEY", "trace-test-credential-key-0123456789")
        service_token = generate_service_token()
        service_client = ServiceClient(
            client_name="trace-exporter-integration",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(service_token),
            service_token_lookup_hash=compute_lookup_hash(service_token),
        )
        db.add(service_client)
        db.commit()
        owner = make_user(db, username="trace_export_owner")
        task = _make_task(db, owner)

        class _ClientAdapter:
            def post(self, url, json=None, headers=None):
                return client.post(url, json=json, headers=headers)

            def close(self) -> None:
                # The pytest fixture owns the TestClient lifecycle.
                pass

        exporter = TraceExporter(
            str(client.base_url),
            token_provider=lambda: service_token,
            start_worker=False,
            client_factory=_ClientAdapter,
            task_id=task.id,
            user_identity=owner.username,
        )
        exporter.enqueue(
            task.trace_id,
            _span_payload("sdk-to-csp", span_type="agent.run.finished"),
        )
        exporter.flush()

        assert exporter.stats() == {
            "queued": 0,
            "dropped": 0,
            "sent": 1,
            "failed": 0,
        }
        db.expire_all()
        row = db.query(TraceSpan).filter_by(
            trace_id=task.trace_id,
            # Query uses the task-owned trace, never a free-floating id.
            span_id="sdk-to-csp",
        ).one()
        assert row.producer == "router"


    def test_happy_path_persists_and_links_task(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="tr_happy")
        token = login(client, "tr_happy")
        task = _make_task(db, user)

        resp = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(token),
            json={"spans": [
                _span_payload("s1", started_at="2026-07-02T00:00:00Z"),
                _span_payload("s2", span_type="agent.run.finished"),
            ]},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"accepted": 2, "duplicates": 0}

        db.expire_all()
        rows = (
            db.query(TraceSpan)
            .filter(TraceSpan.trace_id == task.trace_id)
            .all()
        )
        assert len(rows) == 2
        assert all(r.task_id == task.id for r in rows)
        # user JWT caller → producer "proxy" (no user producer role exists).
        assert all(r.producer == "proxy" for r in rows)

    def test_idempotent_duplicates_across_and_within_batch(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="tr_dup")
        token = login(client, "tr_dup")
        task = _make_task(db, user)

        r1 = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(token),
            json={"spans": [_span_payload("dup"), _span_payload("dup")]},
        )
        # In-batch duplicate: first accepted, second ignored.
        assert r1.json() == {"accepted": 1, "duplicates": 1}

        r2 = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(token),
            json={"spans": [_span_payload("dup")]},
        )
        # Already persisted: idempotent ignore.
        assert r2.json() == {"accepted": 0, "duplicates": 1}

        db.expire_all()
        assert (
            db.query(TraceSpan)
            .filter(TraceSpan.trace_id == task.trace_id)
            .count()
            == 1
        )

    def test_oversized_batch_returns_413(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="tr_big")
        token = login(client, "tr_big")
        task = _make_task(db, user)
        spans = [_span_payload(f"s{i}") for i in range(257)]

        resp = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(token),
            json={"spans": spans},
        )
        assert resp.status_code == 413

    def test_span_trace_id_mismatch_returns_422_listing_indices(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="tr_bad")
        token = login(client, "tr_bad")
        task = _make_task(db, user)

        resp = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(token),
            json={"spans": [
                _span_payload("ok"),
                _span_payload("bad", trace_id="some-other-trace"),
            ]},
        )
        assert resp.status_code == 422
        assert "索引" in resp.json()["detail"]
        assert "1" in resp.json()["detail"]

    def test_empty_batch_returns_422(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="tr_empty")
        token = login(client, "tr_empty")
        task = _make_task(db, user)
        resp = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(token),
            json={"spans": []},
        )
        assert resp.status_code == 422

    def test_anonymous_caller_returns_401(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="tr_anon")
        task = _make_task(db, user)
        resp = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            json={"spans": [_span_payload("s1")]},
        )
        assert resp.status_code == 401

    def test_traceless_ingest_without_task_fails_closed(
        self, client: TestClient, db: Session
    ):
        """A free-floating trace has no owner/classification authority."""
        make_user(db, username="tr_notask")
        token = login(client, "tr_notask")
        resp = client.post(
            "/v1/traces/free-floating-trace/spans",
            headers=_bearer(token),
            json={"spans": [_span_payload("s1")]},
        )
        assert resp.status_code == 404
        db.expire_all()
        assert db.query(TraceSpan).filter(
            TraceSpan.trace_id == "free-floating-trace"
        ).count() == 0

    def test_foreign_owner_cannot_ingest_and_task_classification_is_floor(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="tr_ingest_owner")
        other = make_user(db, username="tr_ingest_other")
        task = _make_task(db, owner)
        task.classification_level = "機密"
        db.commit()

        denied = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(_jwt(other)),
            json={"spans": [_span_payload("foreign")]},
        )
        assert denied.status_code == 403
        accepted = client.post(
            f"/v1/traces/{task.trace_id}/spans",
            headers=_bearer(_jwt(owner)),
            json={"spans": [
                _span_payload("floor"),
                _span_payload("raised", classification_level="絕對機密"),
            ]},
        )
        assert accepted.status_code == 202
        db.expire_all()
        rows = {r.span_id: r for r in db.query(TraceSpan).filter_by(
            trace_id=task.trace_id
        )}
        assert rows["floor"].classification_level == "機密"
        assert rows["raised"].classification_level == "絕對機密"


class TestQuery:
    def test_requester_gets_flat_ordered_spans(
        self, client: TestClient, db: Session
    ):
        issuer = make_user(db, username="tr_get_issuer", role="admin")
        user = make_user(db, username="tr_get")
        token = login(client, "tr_get")
        task = _make_task(db, user)
        _grant_trace_read_clearance(
            db,
            subject=user,
            issuer=issuer,
        )
        # Seed out of order; expect sort by started_at then span_id.
        _seed_span(db, task.trace_id, "later", task_id=task.id,
                   started_at=datetime(2026, 7, 2, 3, 0, 0))
        _seed_span(db, task.trace_id, "earlier", task_id=task.id,
                   started_at=datetime(2026, 7, 2, 1, 0, 0))
        _seed_span(db, task.trace_id, "middle", task_id=task.id,
                   started_at=datetime(2026, 7, 2, 2, 0, 0))

        resp = client.get(
            f"/api/traces/{task.trace_id}", headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["trace_id"] == task.trace_id
        assert data["task_id"] == task.id
        assert [s["span_id"] for s in data["spans"]] == [
            "earlier", "middle", "later",
        ]

    def test_foreign_user_forbidden(self, client: TestClient, db: Session):
        owner = make_user(db, username="tr_owner")
        make_user(db, username="tr_other")
        token = login(client, "tr_other")
        task = _make_task(db, owner)
        _seed_span(db, task.trace_id, "s1", task_id=task.id)

        resp = client.get(
            f"/api/traces/{task.trace_id}", headers=_bearer(token)
        )
        assert resp.status_code == 403

    def test_admin_task_access_requires_data_clearance(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="tr_owner2")
        admin = make_user(db, username="tr_admin", role="admin")
        token = login(client, "tr_admin")
        task = _make_task(db, owner)
        _seed_span(db, task.trace_id, "s1", task_id=task.id)

        url = f"/api/traces/{task.trace_id}"
        assert client.get(url, headers=_bearer(token)).status_code == 403

        _grant_trace_read_clearance(
            db,
            subject=admin,
            issuer=admin,
        )
        assert client.get(url, headers=_bearer(token)).status_code == 200

    def test_unknown_trace_returns_404(
        self, client: TestClient, db: Session
    ):
        make_user(db, username="tr_admin404", role="admin")
        token = login(client, "tr_admin404")
        resp = client.get(
            "/api/traces/does-not-exist", headers=_bearer(token)
        )
        assert resp.status_code == 404

    def test_taskless_trace_admin_only(
        self, client: TestClient, db: Session
    ):
        # Spans exist for a trace with no owning task → admin/owner only.
        _seed_span(db, "orphan-trace", "s1")
        make_user(db, username="tr_plain")
        admin = make_user(db, username="tr_admin_orphan", role="admin")

        plain_token = login(client, "tr_plain")
        assert client.get(
            "/api/traces/orphan-trace", headers=_bearer(plain_token)
        ).status_code == 403

        admin_token = login(client, "tr_admin_orphan")
        assert client.get(
            "/api/traces/orphan-trace", headers=_bearer(admin_token)
        ).status_code == 403
        _grant_trace_read_clearance(
            db,
            subject=admin,
            issuer=admin,
        )
        assert client.get(
            "/api/traces/orphan-trace", headers=_bearer(admin_token)
        ).status_code == 200


# ── Proxy self-emitted spans ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _dev_ssrf_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm,agent")


@pytest.fixture
def out_of_request_sessions(monkeypatch, db_engine):
    """Point the out-of-request session factories (finalize + span emit) at
    the test engine — both run after the request-scoped dependency session."""
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(task_link, "SessionLocal", factory)
    monkeypatch.setattr(proxy_spans, "SessionLocal", factory)
    return factory


@pytest.fixture
def captured_usage(monkeypatch):
    async def _fake(**kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _fake)
    monkeypatch.setattr(proxy_impl, "enqueue_usage", _fake)


def _trace_spans(db: Session, trace_id: str) -> list[TraceSpan]:
    return db.query(TraceSpan).filter(TraceSpan.trace_id == trace_id).all()


class TestProxyEmittedSpans:
    def test_model_call_emits_dispatch_and_model_spans(
        self, client: TestClient, db: Session, monkeypatch,
        out_of_request_sessions, captured_usage,
    ):
        admin = make_user(db, username="span_admin", role="admin")
        make_model(db, name="span-llm")
        task = _make_task(db, admin)
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)),
                     "X-ANILA-Task-Id": str(task.id)},
            json={"model": "span-llm",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        rows = _trace_spans(db, task.trace_id)
        assert len(rows) == 2
        by_type = {r.span_type: r for r in rows}
        parent = by_type["agent.run.finished"]      # proxy.dispatch
        child = by_type["agent.model_call.finished"]  # model.call
        assert child.parent_span_id == parent.span_id
        assert parent.task_id == task.id and child.task_id == task.id
        assert parent.producer == "proxy"
        assert parent.status == "ok" and child.status == "ok"
        # Non-stream upstream returned usage → total_tokens rides the child.
        assert child.attributes["usage"]["total_tokens"] == 7

    def test_agent_stream_emits_tool_call_child(
        self, client: TestClient, db: Session, monkeypatch,
        out_of_request_sessions, captured_usage,
    ):
        user, _model, agent, now = _ready_agent(db)
        task = _make_task(db, user)
        _patch_stream_client(monkeypatch)
        snapshot = build_registry_snapshot(db, user_id=user.id, now=now)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(user)),
                "X-ANILA-Task-Id": str(task.id),
                "X-ANILA-Registry-Snapshot-Id": snapshot.snapshot_id,
                "X-ANILA-Registry-Snapshot-Revision": snapshot.snapshot_revision,
                "X-ANILA-Registry-Snapshot-Hash": snapshot.snapshot_hash,
                "X-ANILA-Agent-Manifest-Revision": agent.manifest_revision,
                     "X-ANILA-Agent-Manifest-SHA256": agent.manifest_sha256},
            json={"model": agent.name, "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        _ = resp.text  # drain SSE so the stream finalizes

        db.expire_all()
        types = {r.span_type for r in _trace_spans(db, task.trace_id)}
        assert "agent.run.finished" in types       # proxy.dispatch
        assert "agent.tool_call.finished" in types  # agent.call

    def test_legacy_no_task_emits_no_spans(
        self, client: TestClient, db: Session, monkeypatch,
        out_of_request_sessions, captured_usage,
    ):
        admin = make_user(db, username="span_legacy", role="admin")
        make_model(db, name="span-legacy-llm")
        _patch_post_client(monkeypatch)

        resp = client.post(
            "/v1/chat/completions",
            headers=_bearer(_jwt(admin)),
            json={"model": "span-legacy-llm",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        db.expire_all()
        assert db.query(TraceSpan).count() == 0

    def test_upstream_error_records_error_status_spans(
        self, client: TestClient, db: Session, monkeypatch,
        out_of_request_sessions, captured_usage,
    ):
        admin = make_user(db, username="span_err", role="admin")
        make_model(db, name="span-err-llm")
        task = _make_task(db, admin)
        _patch_post_client(monkeypatch, status_code=500)
        monkeypatch.setattr(proxy_service.settings, "PROXY_MAX_RETRIES", 1)
        monkeypatch.setattr(proxy_service.settings, "PROXY_RETRY_BASE_DELAY", 0)

        resp = client.post(
            "/v1/chat/completions",
            headers={**_bearer(_jwt(admin)),
                     "X-ANILA-Task-Id": str(task.id)},
            json={"model": "span-err-llm",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502

        db.expire_all()
        rows = _trace_spans(db, task.trace_id)
        assert rows
        assert all(r.status == "error" for r in rows)
