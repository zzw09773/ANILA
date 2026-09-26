"""派工 JWT 的範圍、分類上限、搜尋可用性與遷移回填。"""

from __future__ import annotations

import re

import pytest
from jose import jwt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api.ingestion.search import resolve_search_principal
from app.config import settings
from app.main import app
from app.models.conversation import Conversation
from app.models.task import Task
from app.services.proxy.dispatch_token import (
    build_dispatch_claims,
    issue_dispatch_token,
)
from app.utils.security import ALGORITHM, get_private_key
from fastapi.security import HTTPAuthorizationCredentials
from tests.conftest import make_agent, make_model, make_user
from tests.test_agent_model_delegation import _RecordingClient
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _mock_upstream(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm,agent")
    _RecordingClient.posts = []
    monkeypatch.setattr(
        "app.services.proxy_service.httpx.AsyncClient",
        lambda *args, **kwargs: _RecordingClient(*args, **kwargs),
    )

# 派工 JWT 可以證明身分的端點。成品寫入與其他資料面不在內。
DISPATCH_JWT_ALLOWLIST = {
    ("POST", "/v1/chat/completions"),
    ("POST", "/api/ingestion/collections/{collection_id}/search"),
    ("POST", "/api/ingestion/collections/{collection_id}/images/search"),
}

# 不驗身分的公開面。2xx 不代表接受了派工 JWT。
PUBLIC_2XX = {
    ("GET", "/health"),
    ("GET", "/docs"),
    ("GET", "/.well-known/jwks.json"),
    ("GET", "/api/banners/public"),
}


def _signed(claims: dict) -> str:
    return jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )


def _token_for(user_id: int, agent_id: int, **extra) -> str:
    claims = build_dispatch_claims(
        user_id=user_id, department=None, agent_id=agent_id
    )
    claims.update(extra)
    return _signed(claims)


@pytest.mark.asyncio
async def test_dispatch_model_call_uses_task_level_from_claims_not_headers(
    client, db: Session
):
    """任務分類來自簽過的派工 claims。請求標頭裡的任務 id 不能把上限放寬。"""
    from tests.test_agent_model_delegation import _approved_agent

    asker = make_user(db, username="ceil-asker", role="user")
    owner = make_user(db, username="ceil-owner", role="developer")
    base = make_model(db, name="ceil-base")
    base.classification_ceiling = "無機密"
    db.commit()
    agent = _approved_agent(db, owner=owner, name="ceil-helper", base=base)
    secret = Task(
        title="機密任務",
        task_type="query",
        requester_user_id=asker.id,
        status="running",
        classification_level="機密",
    )
    decoy = Task(
        title="掩護任務",
        task_type="query",
        requester_user_id=asker.id,
        status="running",
        classification_level="無機密",
    )
    db.add_all([secret, decoy])
    db.commit()
    token = _token_for(asker.id, agent.id, task_id=secret.id)
    resp = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "X-ANILA-Task-Id": str(decoy.id),
        },
        json={
            "model": base.name,
            "messages": [{"role": "user", "content": "機密內容"}],
        },
    )
    assert resp.status_code == 403, resp.text
    assert "機密" in resp.json()["detail"]
    assert _RecordingClient.posts == []


def test_header_task_alone_does_not_raise_the_ceiling(client, db: Session):
    """沒寫進派工 JWT 的任務 id，即使標頭放了高等級任務，也不能拿來擋或放。"""
    from tests.test_agent_model_delegation import _approved_agent

    asker = make_user(db, username="ceil-header", role="user")
    owner = make_user(db, username="ceil-header-owner", role="developer")
    base = make_model(db, name="ceil-header-base")
    base.classification_ceiling = "無機密"
    db.commit()
    agent = _approved_agent(db, owner=owner, name="ceil-header-agent", base=base)
    secret = Task(
        title="只在標頭",
        task_type="query",
        requester_user_id=asker.id,
        status="running",
        classification_level="機密",
    )
    db.add(secret)
    db.commit()
    token = issue_dispatch_token(
        user_id=asker.id, department=None, agent_id=agent.id
    )
    resp = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "X-ANILA-Task-Id": str(secret.id),
        },
        json={
            "model": base.name,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text


def test_conversation_claim_is_enforced_when_there_is_no_task(client, db: Session):
    from tests.test_agent_model_delegation import _approved_agent

    asker = make_user(db, username="ceil-conv", role="user")
    owner = make_user(db, username="ceil-conv-owner", role="developer")
    base = make_model(db, name="ceil-conv-base")
    base.classification_ceiling = "營業秘密"
    db.commit()
    agent = _approved_agent(db, owner=owner, name="ceil-conv-agent", base=base)
    conv = Conversation(user_id=asker.id, title="已列管", classification_level="機密")
    db.add(conv)
    db.commit()
    token = _token_for(asker.id, agent.id, conversation_id=conv.id)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": base.name,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 403, resp.text
    assert "機密" in resp.json()["detail"]
    assert _RecordingClient.posts == []


def test_minted_dispatch_token_carries_task_and_conversation():
    from app.services.proxy.dispatch_token import verify_dispatch_token
    from app.services.proxy.headers import build_agent_headers

    headers = build_agent_headers(
        user_id=4,
        department=8,
        agent_id=15,
        task_id=21,
        trace_id="trace-x",
        conversation_id=34,
    )
    token = headers["Authorization"].removeprefix("Bearer ").strip()
    claims = verify_dispatch_token(token)
    assert claims is not None
    assert claims["task_id"] == 21
    assert claims["conversation_id"] == 34


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def test_dispatch_jwt_is_rejected_outside_chat_and_search(client, db: Session):
    owner = make_user(db, username="scope-owner", role="developer")
    agent = make_agent(db, owner, name="scope-agent", approval_status="approved")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    headers = {"Authorization": f"Bearer {token}"}
    # 有效的成品寫入。若派工 JWT 被當成服務憑證，這裡會是 201。
    created = client.post(
        "/v1/artifact-jobs",
        headers=headers,
        json={
            "job_id": "dispatch-must-not-write",
            "artifact_type": "report",
            "requester_user_id": owner.id,
        },
    )
    assert created.status_code in (401, 403), created.text
    registered = client.post(
        "/v1/artifacts",
        headers=headers,
        json={
            "artifact_type": "report",
            "title": "不該寫入",
            "storage_ref": "store://nope",
            "task_id": 1,
        },
    )
    assert registered.status_code in (401, 403), registered.text

    accepted: list[str] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            key = (method, path)
            if key in DISPATCH_JWT_ALLOWLIST or key in PUBLIC_2XX:
                continue
            kwargs = {}
            if method in {"POST", "PUT", "PATCH"}:
                kwargs["json"] = {}
            resp = client.request(method, _fill(path), headers=headers, **kwargs)
            if 200 <= resp.status_code < 300:
                accepted.append(f"{method} {path} -> {resp.status_code}")
    assert accepted == []


def test_search_dispatch_rejects_unapproved_and_unavailable_agents(db: Session):
    from fastapi import HTTPException

    owner = make_user(db, username="search-gate-owner", role="developer")
    pending = make_agent(db, owner, name="search-pending", approval_status="registered")
    offline = make_agent(db, owner, name="search-offline", approval_status="approved")
    offline.unavailable_reason = "base_model_offline"
    db.commit()

    def _open(agent_id: int):
        token = issue_dispatch_token(
            user_id=owner.id, department=None, agent_id=agent_id
        )
        return resolve_search_principal(
            request=MagicMock(),
            credentials=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token
            ),
            db=db,
        )

    with pytest.raises(HTTPException) as pending_exc:
        _open(pending.id)
    assert pending_exc.value.status_code == 403
    assert "核准" in pending_exc.value.detail

    with pytest.raises(HTTPException) as offline_exc:
        _open(offline.id)
    assert offline_exc.value.status_code == 403
    assert offline_exc.value.detail == "此助手暫時無法使用"


def _reason_rows(conn) -> dict[int, str | None]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            text("SELECT id, unavailable_reason FROM agents ORDER BY id")
        )
    }


def test_unavailable_reason_backfill_is_idempotent():
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from migrations.versions import r1_0049_agent_unavailable_reason as mig

    expected = {
        1: "base_model_offline",  # 已核准、底層模型停用
        2: None,  # 已核准、底層模型仍啟用
        3: "base_model_offline",  # 已核准、綁定已遺失
        4: "base_model_offline",  # 已核准、模型列不存在
        5: None,  # 尚未核准，不進派工清單
    }
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE model_registry (
                    id INTEGER PRIMARY KEY,
                    is_active BOOLEAN NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE agents (
                    id INTEGER PRIMARY KEY,
                    approval_status VARCHAR(30) NOT NULL,
                    base_model_id INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO model_registry (id, is_active) VALUES
                  (1, 0),
                  (2, 1)
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agents (id, approval_status, base_model_id) VALUES
                  (1, 'approved', 1),
                  (2, 'approved', 2),
                  (3, 'approved', NULL),
                  (4, 'approved', 99),
                  (5, 'registered', 1)
                """
            )
        )
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig.upgrade()
        assert _reason_rows(conn) == expected
        mig.backfill_unavailable_reason(conn)
        mig.backfill_unavailable_reason(conn)
        assert _reason_rows(conn) == expected
