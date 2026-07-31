"""OE-1 end-to-end: register → admin assigns → user invokes.

No connection-test / trace-test / security-review step. Assigning a
registered agent auto-approves it (SYSTEM-MAP eight-step table).
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services import proxy_service
from tests.conftest import login, make_agent, make_model, make_user


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _PostResponse:
    def __init__(self, payload, status_code: int = 200):
        import json as _json

        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _PostClient:
    last_url: str | None = None
    last_headers: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_url = url
        type(self).last_headers = dict(headers or {})
        return _PostResponse(
            {
                "id": "chatcmpl-oe1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "oe1-ok"},
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


def _patch_post_client(monkeypatch):
    _PostClient.last_url = None
    _PostClient.last_headers = {}
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _PostClient(*a, **k)
    )


@pytest.fixture(autouse=True)
def _env_allowances(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")


class TestOe1RegisterAssignInvoke:
    def test_register_assign_invoke_without_gates(
        self, client: TestClient, db: Session, monkeypatch
    ):
        make_user(db, username="oe1_dev", role="developer")
        user = make_user(db, username="oe1_user", role="user")
        make_user(db, username="oe1_admin", role="admin")
        model = make_model(db, name="oe1-base")

        # 1) Developer registers — lands registered, no test run.
        dev_token = login(client, "oe1_dev")
        reg = client.post(
            "/api/agents/register",
            headers=_bearer(dev_token),
            json={
                "name": "oe1-live-agent",
                "endpoint_url": "http://agent:9100",
                "description_for_router": "OE-1 live path agent for register-assign-invoke",
                "base_model_id": model.id,
            },
        )
        assert reg.status_code == 200, reg.text
        agent = reg.json()
        assert agent["approval_status"] == "registered"
        agent_id = agent["id"]

        # 2) Admin assigns the registered agent to the user (auto-approves).
        admin_token = login(client, "oe1_admin")
        assign = client.put(
            f"/api/users/{user.id}/allowed-agents",
            headers=_bearer(admin_token),
            json={"agent_ids": [agent_id]},
        )
        assert assign.status_code == 200, assign.text

        listed = client.get("/api/agents", headers=_bearer(admin_token))
        assert listed.status_code == 200
        row = next(a for a in listed.json() if a["id"] == agent_id)
        assert row["approval_status"] == "approved"

        # 3) User invokes via data-plane chat completions (model = agent name).
        _patch_post_client(monkeypatch)
        user_token = login(client, "oe1_user")
        invoke = client.post(
            "/v1/chat/completions",
            headers=_bearer(user_token),
            json={
                "model": "oe1-live-agent",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
        assert invoke.status_code == 200, invoke.text
        body = invoke.json()
        assert body["choices"][0]["message"]["content"] == "oe1-ok"
        assert _PostClient.last_url and "agent:9100" in _PostClient.last_url

    def test_classification_ceiling_still_refuses(self, db: Session):
        """Ceiling still refuses over-ceiling levels (behaviour unchanged)."""
        from fastapi import HTTPException

        from app.models.conversation import Conversation
        from app.services.proxy.ceiling import enforce_agent_ceiling

        class _Caller:
            def __init__(self, user):
                self.user = user
                self.api_key_id = None

        owner = make_user(db, "oe1_ceil_owner", role="developer")
        caller = make_user(db, "oe1_ceil_caller")
        agent = make_agent(
            db, owner, name="oe1-ceil-agent", approval_status="approved"
        )
        agent.classification_ceiling = "無機密"
        db.commit()
        conv = Conversation(user_id=caller.id, title="c")
        db.add(conv)
        db.commit()
        conv.classification_level = "機密"
        db.commit()

        with pytest.raises(HTTPException) as exc:
            enforce_agent_ceiling(
                db,
                agent=agent,
                caller=_Caller(caller),
                task_ctx=None,
                conv_id_int=conv.id,
            )
        assert exc.value.status_code == 403
