# -*- coding: utf-8 -*-
"""P2.4 / A01 — authorisation gaps + enumeration collapse.

Each finding has a wrong-caller refusal AND a right-caller success.
Artifact read vs export share ``ensure_artifact_access`` — one test
locks that they cannot drift.

Revert notes (turn green → red):
  H1: drop ensure_artifact_access from export_artifact
  H2: restore ``if conversation_id is not None``-only gate in upload
  H3: drop ensure_agent_session_owner from resume_agent_session
  H4: drop get_conversation(for_write=True) from create_handoff
  E1: restore 403 in _check_access / _check_read_access
  E2: restore 403 in get_agent / get_service
  E3: restore 403 in cancel_handoff
"""
from __future__ import annotations

import io
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.conversation import Conversation
from app.models.handoff import Handoff
from app.models.message import Message
from app.services.agent_session_owner_service import ensure_agent_session_owner
from app.utils.security import create_access_token
from tests.conftest import make_agent, make_user

SVC_TOKEN = "svc-a01-test-token"
_SVC = {"X-CSP-Service-Token": SVC_TOKEN}


@pytest.fixture(autouse=True)
def _svc_token(monkeypatch):
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", SVC_TOKEN)
    yield


def _bearer(user) -> dict:
    token = create_access_token({
        "sub": str(user.id), "username": user.username,
        "role": user.role, "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _make_conv(db: Session, user, title: str = "a01") -> Conversation:
    conv = Conversation(user_id=user.id, title=title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _make_msg(db: Session, conv: Conversation, user) -> Message:
    msg = Message(
        conversation_id=conv.id,
        role="user", content="hi",
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _seed_artifact(client: TestClient, db: Session, owner, level: str = "無機密"):
    from app.models.task import Task

    task = Task(
        title="a01", task_type="query", requester_user_id=owner.id,
        status="submitted", classification_level=level,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    resp = client.post(
        "/v1/artifacts", headers=_SVC,
        json={
            "artifact_type": "report", "title": "a01",
            "storage_ref": "store://a01.pdf", "task_id": task.id,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["artifact_id"]


# ── H1 artifact export ──────────────────────────────────────────────────────


class TestH1ArtifactExportOwnership:
    def test_foreign_user_export_refused(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h1_owner")
        other = make_user(db, username="a01_h1_other")
        art_id = _seed_artifact(client, db, owner)
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_bearer(other),
            json={"target_classification_floor": "無機密"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "無權存取此 artifact"

    def test_owner_export_still_succeeds(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h1_ok")
        art_id = _seed_artifact(client, db, owner)
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_bearer(owner),
            json={"target_classification_floor": "無機密"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["decision"] == "allow"

    def test_service_token_export_unaffected(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h1_svc")
        art_id = _seed_artifact(client, db, owner)
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_SVC,
            json={"target_classification_floor": "無機密"},
        )
        assert resp.status_code == 201, resp.text

    def test_read_and_export_same_decision(
        self, client: TestClient, db: Session,
    ):
        """Read face and export face must not drift apart."""
        owner = make_user(db, username="a01_h1_sync_o")
        other = make_user(db, username="a01_h1_sync_x")
        art_id = _seed_artifact(client, db, owner, level="營業秘密")

        read_owner = client.get(
            f"/api/artifacts/{art_id}", headers=_bearer(owner),
        )
        export_owner = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_bearer(owner),
            json={"target_classification_floor": "無機密"},
        )
        assert read_owner.status_code == 200
        assert export_owner.status_code == 201

        read_other = client.get(
            f"/api/artifacts/{art_id}", headers=_bearer(other),
        )
        export_other = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_bearer(other),
            json={"target_classification_floor": "無機密"},
        )
        assert read_other.status_code == 403
        assert export_other.status_code == 403
        assert read_other.json()["detail"] == export_other.json()["detail"]


# ── H2 attachment message_id ────────────────────────────────────────────────


class TestH2AttachmentMessageGate:
    def test_foreign_message_id_refused(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h2_owner")
        other = make_user(db, username="a01_h2_other")
        conv = _make_conv(db, owner)
        msg = _make_msg(db, conv, owner)
        resp = client.post(
            "/api/attachments",
            headers=_bearer(other),
            data={"message_id": str(msg.id)},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        assert resp.status_code == 404

    def test_owner_message_id_still_succeeds(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h2_ok")
        conv = _make_conv(db, owner)
        msg = _make_msg(db, conv, owner)
        resp = client.post(
            "/api/attachments",
            headers=_bearer(owner),
            data={"message_id": str(msg.id)},
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["message_id"] == msg.id
        assert resp.json()["conversation_id"] == conv.id

    def test_mismatched_conversation_and_message_refused(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h2_mis")
        conv_a = _make_conv(db, owner, title="a")
        conv_b = _make_conv(db, owner, title="b")
        msg = _make_msg(db, conv_a, owner)
        resp = client.post(
            "/api/attachments",
            headers=_bearer(owner),
            data={
                "message_id": str(msg.id),
                "conversation_id": str(conv_b.id),
            },
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        assert resp.status_code == 400

    def test_foreign_message_with_own_conversation_is_404_not_400(
        self, client: TestClient, db: Session,
    ):
        """Access before mismatch — no message-id existence oracle."""
        owner = make_user(db, username="a01_h2_or_o")
        other = make_user(db, username="a01_h2_or_x")
        victim_conv = _make_conv(db, owner)
        msg = _make_msg(db, victim_conv, owner)
        own_conv = _make_conv(db, other, title="own")
        foreign = client.post(
            "/api/attachments",
            headers=_bearer(other),
            data={
                "message_id": str(msg.id),
                "conversation_id": str(own_conv.id),
            },
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        missing = client.post(
            "/api/attachments",
            headers=_bearer(other),
            data={
                "message_id": "999999",
                "conversation_id": str(own_conv.id),
            },
            files={"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        assert foreign.status_code == 404
        assert missing.status_code == 404
        assert foreign.json()["detail"] == missing.json()["detail"]


# ── H3 agent session resume ─────────────────────────────────────────────────


class TestH3AgentSessionOwnership:
    def test_foreign_resume_refused(
        self, client: TestClient, db: Session, monkeypatch,
    ):
        owner = make_user(db, username="a01_h3_owner")
        other = make_user(db, username="a01_h3_other")
        agent = make_agent(db, owner, name="a01-h3-agent")
        agent.approval_status = "approved"
        db.commit()
        # Grant other permission to USE the agent (so the gap is ownership,
        # not agent permission).
        from app.models.agent import UserAgentPermission
        db.add(UserAgentPermission(user_id=other.id, agent_id=agent.id))
        db.commit()

        ensure_agent_session_owner(
            db, session_id="sid-a01-h3", owner_user_id=owner.id,
        )
        resp = client.post(
            f"/v1/agents/{agent.name}/sessions/sid-a01-h3/answer",
            headers=_bearer(other),
            json={"interrupt_id": "i1", "answer": "nope"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Session not found"

    def test_owner_resume_still_reaches_upstream(
        self, client: TestClient, db: Session, monkeypatch,
    ):
        owner = make_user(db, username="a01_h3_ok")
        agent = make_agent(db, owner, name="a01-h3-ok")
        agent.approval_status = "approved"
        db.commit()
        from app.models.agent import UserAgentPermission
        db.add(UserAgentPermission(user_id=owner.id, agent_id=agent.id))
        db.commit()

        ensure_agent_session_owner(
            db, session_id="sid-a01-ok", owner_user_id=owner.id,
        )

        calls: list[str] = []

        class _FakeResp:
            status_code = 200

            async def aread(self):
                return b""

            async def aiter_lines(self):
                yield 'data: {"ok": true}'
                yield ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, **kwargs):
                calls.append(url)
                return _FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        # SSRF guard would refuse fake endpoint_url — skip it.
        monkeypatch.setattr(
            "app.services.proxy_service._guard_outbound", lambda *a, **k: None,
        )

        resp = client.post(
            f"/v1/agents/{agent.name}/sessions/sid-a01-ok/answer",
            headers=_bearer(owner),
            json={"interrupt_id": "i1", "answer": "yes"},
        )
        assert resp.status_code == 200, resp.text
        assert calls, "owner resume must reach upstream"

    def test_chat_face_binds_anila_session_id(
        self, client: TestClient, db: Session, monkeypatch,
    ):
        """Production write path: chat with anila_session_id must create the row.

        Revert: drop ensure_agent_session_owner from the agent chat branch
        after enforce_agent_ceiling → this goes red; pre-seeded resume tests
        would still pass.
        """
        from app.models.agent import UserAgentPermission
        from app.models.agent_session_owner import AgentSessionOwner
        from app.services.proxy import service as proxy_impl

        owner = make_user(db, username="a01_h3_bind")
        agent = make_agent(db, owner, name="a01-h3-bind")
        agent.approval_status = "approved"
        db.commit()
        db.add(UserAgentPermission(user_id=owner.id, agent_id=agent.id))
        db.commit()

        monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
        monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")

        class _PostResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = "{}"

            def json(self):
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}}
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
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _PostResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _Client)

        async def _noop_usage(**kwargs):
            return None

        monkeypatch.setattr(proxy_impl, "enqueue_usage", _noop_usage)
        monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _noop_usage)

        sid = "sid-chat-bind-a01"
        assert (
            db.query(AgentSessionOwner)
            .filter(AgentSessionOwner.session_id == sid)
            .first()
            is None
        )
        resp = client.post(
            "/v1/chat/completions",
            headers=_bearer(owner),
            json={
                "model": agent.name,
                "messages": [{"role": "user", "content": "hi"}],
                "anila_session_id": sid,
                "stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        row = (
            db.query(AgentSessionOwner)
            .filter(AgentSessionOwner.session_id == sid)
            .one()
        )
        assert row.owner_user_id == owner.id


# ── H4 handoff create ───────────────────────────────────────────────────────


class TestH4HandoffCreateOwnership:
    def test_foreign_conversation_refused(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h4_owner")
        other = make_user(db, username="a01_h4_other")
        peer = make_user(db, username="a01_h4_peer")
        conv = _make_conv(db, owner)
        resp = client.post(
            "/api/handoffs",
            headers=_bearer(other),
            json={
                "conversation_id": conv.id,
                "to_user_id": peer.id,
                "note": "stolen",
            },
        )
        assert resp.status_code == 404
        assert db.query(Handoff).count() == 0

    def test_owner_handoff_still_succeeds(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_h4_ok")
        peer = make_user(db, username="a01_h4_ok_peer")
        conv = _make_conv(db, owner)
        resp = client.post(
            "/api/handoffs",
            headers=_bearer(owner),
            json={
                "conversation_id": conv.id,
                "to_user_id": peer.id,
                "note": "please",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["conversation_id"] == conv.id


# ── E1 conversation enumeration ─────────────────────────────────────────────


class TestE1ConversationEnumeration:
    def test_foreign_get_is_404_not_403(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_e1_owner")
        other = make_user(db, username="a01_e1_other")
        conv = _make_conv(db, owner)
        missing = client.get(
            "/api/conversations/999999", headers=_bearer(other),
        )
        foreign = client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(other),
        )
        assert missing.status_code == 404
        assert foreign.status_code == 404
        assert missing.json()["detail"] == foreign.json()["detail"]

    def test_owner_get_still_succeeds(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_e1_ok")
        conv = _make_conv(db, owner)
        resp = client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(owner),
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == conv.id


# ── E2 agent / service enumeration ──────────────────────────────────────────


class TestE2AgentServiceEnumeration:
    def test_foreign_agent_get_is_404(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_e2_own", role="developer")
        other = make_user(db, username="a01_e2_oth", role="developer")
        agent = make_agent(db, owner, name="a01-e2-agent")
        missing = client.get(
            "/api/agents/999999", headers=_bearer(other),
        )
        foreign = client.get(
            f"/api/agents/{agent.id}", headers=_bearer(other),
        )
        assert missing.status_code == 404
        assert foreign.status_code == 404
        assert missing.json()["detail"] == foreign.json()["detail"]

    def test_owner_agent_get_still_succeeds(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_e2_ok", role="developer")
        agent = make_agent(db, owner, name="a01-e2-ok")
        resp = client.get(
            f"/api/agents/{agent.id}", headers=_bearer(owner),
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == agent.id


# ── E3 handoff cancel enumeration ───────────────────────────────────────────


class TestE3HandoffCancelEnumeration:
    def test_foreign_cancel_is_404(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_e3_owner")
        other = make_user(db, username="a01_e3_other")
        peer = make_user(db, username="a01_e3_peer")
        conv = _make_conv(db, owner)
        created = client.post(
            "/api/handoffs",
            headers=_bearer(owner),
            json={"conversation_id": conv.id, "to_user_id": peer.id},
        )
        assert created.status_code == 201, created.text
        hid = created.json()["id"]

        missing = client.post(
            "/api/handoffs/999999/cancel", headers=_bearer(other),
        )
        foreign = client.post(
            f"/api/handoffs/{hid}/cancel", headers=_bearer(other),
        )
        assert missing.status_code == 404
        assert foreign.status_code == 404
        assert missing.json()["detail"] == foreign.json()["detail"]

    def test_owner_cancel_still_succeeds(
        self, client: TestClient, db: Session,
    ):
        owner = make_user(db, username="a01_e3_ok")
        peer = make_user(db, username="a01_e3_ok_peer")
        conv = _make_conv(db, owner)
        created = client.post(
            "/api/handoffs",
            headers=_bearer(owner),
            json={"conversation_id": conv.id, "to_user_id": peer.id},
        )
        assert created.status_code == 201, created.text
        hid = created.json()["id"]
        resp = client.post(
            f"/api/handoffs/{hid}/cancel", headers=_bearer(owner),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"
