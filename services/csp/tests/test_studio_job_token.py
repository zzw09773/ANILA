"""Studio 工作委託權杖：使用者存取權杖過期後，工作仍以同一人的身分呼叫 CSP。

簽章走既有 RS256 金鑰圈。audience 是 studio。claims 帶 user_id、job_id、
classification。壽命覆蓋長工作（60 分鐘）。撤銷清單（token_version）
仍拒絕這張權杖。授權不換成服務帳號：知識庫範圍、模型授權、用量都算在
該使用者身上。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session, sessionmaker

from app.models.ingestion import IngestionCollection
from app.models.token_usage import TokenUsage
from app.models.user import UserModelPermission
from app.services.auth_service import create_tokens
from app.services.token_revocation import commit_token_revocation
from app.utils.security import ALGORITHM, get_kid, get_private_key, get_public_key
from tests.conftest import make_model, make_user


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _collection(db: Session, owner, *, name: str, level: str) -> IngestionCollection:
    now = datetime.now(timezone.utc)
    coll = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model="nv-embed",
        embedding_dim=8,
        status="active",
        created_by=owner.id,
        classification_level=level,
        created_at=now,
        updated_at=now,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _mint(client: TestClient, access: str, *, job_id: str, collection_id: int):
    return client.post(
        "/api/studio/job-tokens",
        headers=_auth(access),
        json={"job_id": job_id, "collection_id": collection_id},
    )


def _expired_access(user, db: Session) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "tv": int(user.token_version or 0),
            "type": "access",
            "iat": int((now - timedelta(minutes=5)).timestamp()),
            "exp": int((now - timedelta(minutes=1)).timestamp()),
        },
        get_private_key(db),
        algorithm=ALGORITHM,
        headers={"kid": get_kid(db), "typ": "JWT"},
    )


def test_job_token_outlives_expired_user_access_and_keeps_collection_scope(
    client: TestClient, db: Session
):
    """長工作：使用者的存取權杖已經過期，工作權杖仍讀得到他自己的庫，讀不到別人的。"""
    owner = make_user(db, username="studio-job-owner")
    other = make_user(db, username="studio-job-other")
    own = _collection(db, owner, name="own-kb", level="營業秘密")
    foreign = _collection(db, other, name="foreign-kb", level="機密")
    access = create_tokens(owner, db)["access_token"]

    minted = _mint(client, access, job_id="j_longjob", collection_id=own.id)
    assert minted.status_code == 200, minted.text
    body = minted.json()
    token = body["token"]
    assert body["expires_in"] == 60 * 60
    assert body["token_type"] == "bearer"

    claims = jwt.decode(
        token,
        get_public_key(db),
        algorithms=[ALGORITHM],
        audience="studio",
        issuer="anila-csp",
    )
    assert claims["user_id"] == owner.id
    assert claims["job_id"] == "j_longjob"
    assert claims["classification"] == "營業秘密"
    assert claims["tv"] == int(owner.token_version or 0)
    assert int(claims["exp"]) - int(claims["iat"]) == 60 * 60
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"] == get_kid(db)

    expired = _expired_access(owner, db)
    denied = client.get(
        f"/api/ingestion/collections/{own.id}", headers=_auth(expired)
    )
    assert denied.status_code == 401, denied.text

    allowed = client.get(
        f"/api/ingestion/collections/{own.id}", headers=_auth(token)
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["classification_level"] == "營業秘密"
    assert allowed.json()["created_by"] == owner.id

    cross = client.get(
        f"/api/ingestion/collections/{foreign.id}", headers=_auth(token)
    )
    assert cross.status_code == 403, cross.text


def test_revoked_user_job_token_is_rejected(
    client: TestClient, db: Session, monkeypatch
):
    """撤銷（token_version 與撤銷清單）之後，已發出的工作權杖不能再用。"""
    monkeypatch.setattr(
        "app.services.token_revocation_publisher.publish_revocation_sync",
        lambda *args, **kwargs: None,
    )
    owner = make_user(db, username="studio-job-revoked")
    own = _collection(db, owner, name="revoked-kb", level="無機密")
    access = create_tokens(owner, db)["access_token"]
    minted = _mint(client, access, job_id="j_revoke", collection_id=own.id)
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]

    owner.token_version = int(owner.token_version or 0) + 1
    db.commit()
    commit_token_revocation(db, owner)

    rejected = client.get(
        f"/api/ingestion/collections/{own.id}", headers=_auth(token)
    )
    assert rejected.status_code == 401, rejected.text

    again = _mint(client, token, job_id="j_renew", collection_id=own.id)
    assert again.status_code in (401, 403), again.text


def test_job_token_cannot_change_password_or_remint(
    client: TestClient, db: Session
):
    owner = make_user(db, username="studio-job-pwd")
    own = _collection(db, owner, name="pwd-kb", level="無機密")
    access = create_tokens(owner, db)["access_token"]
    minted = _mint(client, access, job_id="j_pwd", collection_id=own.id)
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]

    remint = _mint(client, token, job_id="j_pwd2", collection_id=own.id)
    assert remint.status_code == 403, remint.text

    changed = client.put(
        "/api/auth/password",
        headers=_auth(token),
        json={"current_password": "password", "new_password": "another-password"},
    )
    assert changed.status_code == 403, changed.text
    db.refresh(owner)
    assert int(owner.token_version or 0) == 0


def test_job_token_honors_model_grant_and_bills_the_user(
    client: TestClient, db: Session, db_engine, monkeypatch
):
    """沒有模型授權就是 403；有授權時用量列的 user_id 是這個人，不是服務帳號。"""
    from app.api import proxy as proxy_api
    from app.services import proxy_service, usage_writer
    from tests.test_proxy_nonstream_usage import _AgentClient

    owner = make_user(db, username="studio-job-bill")
    own = _collection(db, owner, name="bill-kb", level="密")
    model = make_model(db, name="studio-job-llm")
    access = create_tokens(owner, db)["access_token"]
    minted = _mint(client, access, job_id="j_bill", collection_id=own.id)
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]
    claims = jwt.get_unverified_claims(token)
    assert claims["classification"] == "密"

    denied = client.post(
        "/v1/chat/completions",
        headers={**_auth(token), "X-ANILA-Request-Source": "studio"},
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "簡報"}],
        },
    )
    assert denied.status_code == 403, denied.text
    assert "無權" in denied.json()["detail"]

    db.add(UserModelPermission(user_id=owner.id, model_id=model.id))
    db.commit()

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **kw: _AgentClient(*a, **kw)
    )
    monkeypatch.setattr(proxy_service, "build_agent_headers", lambda **kwargs: {})
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)
    monkeypatch.setattr(
        usage_writer,
        "SessionLocal",
        sessionmaker(bind=db_engine, expire_on_commit=False),
    )
    # 整包測試跑完後，全域用量佇列可能綁在別的 event loop 上。
    # 這則測試用自己的佇列，避免把別人的 95 筆或已關閉的 loop 算進來。
    isolated: asyncio.Queue = asyncio.Queue()

    def isolated_queue() -> asyncio.Queue:
        return isolated

    monkeypatch.setattr(usage_writer, "get_usage_queue", isolated_queue)
    monkeypatch.setattr(usage_writer, "_usage_queue", isolated)

    allowed = client.post(
        "/v1/chat/completions",
        headers={**_auth(token), "X-ANILA-Request-Source": "studio"},
        json={
            "model": model.name,
            "messages": [{"role": "user", "content": "簡報"}],
        },
    )
    assert allowed.status_code == 200, allowed.text

    queued = isolated.get_nowait()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(usage_writer._flush_batch([queued]))
    finally:
        loop.close()
    db.expire_all()
    row = db.query(TokenUsage).filter(TokenUsage.user_id == owner.id).one()
    assert row.api_key_id is None
    assert row.request_type == "studio"
    assert row.model_id == model.id
