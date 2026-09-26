# -*- coding: utf-8 -*-
"""P2.1 W2 — in-task agent→CSP callbacks authenticate with dispatch JWT.

Acceptance locks:
(a) valid dispatch JWT authenticates collection search, scoped to bound set
(b) JWT for agent A cannot search agent B's collections
(c) expired JWT rejected
(d) tampered JWT rejected
(e) bare ``csk-…`` rejected on converted paths (search + artifacts)
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

import app.api.ingestion.search as search_mod
from app.api.ingestion.search import (
    _enforce_agent_collection_scope,
    resolve_search_principal,
)
from app.config import settings
from app.models.ingestion import IngestionCollection
from app.services.agent_collection_bindings import set_bound_collection_ids
from jose import jwt as jose_jwt

from app.models.artifact import Artifact, ArtifactJob
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.services.proxy.dispatch_token import (
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
    DISPATCH_TOKEN_TTL_MINUTES,
    build_dispatch_claims,
    issue_dispatch_token,
    verify_dispatch_token,
)
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from app.utils.security import ALGORITHM, get_private_key
from tests.conftest import make_agent, make_user


# ── helpers ──────────────────────────────────────────────────────────────────


def _collection(db, owner, name: str) -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model="nv-embed",
        embedding_dim=4000,
        status="active",
        created_by=owner.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


_DISPATCH_NOT_A_SERVICE_CREDENTIAL = "派工 JWT 僅能用於聊天與知識庫搜尋"


def _assert_dispatch_jwt_not_a_service_credential(resp) -> None:
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == _DISPATCH_NOT_A_SERVICE_CREDENTIAL


def _bearer_creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _resolve(db, token: str):
    return resolve_search_principal(
        request=MagicMock(),
        credentials=_bearer_creds(token),
        db=db,
    )


def _tamper_claim(token: str, claim: str, new_value) -> str:
    header_b64, payload_b64, sig_b64 = token.split(".")
    pad = "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    claims[claim] = new_value
    new_payload = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header_b64}.{new_payload}.{sig_b64}"


def _expired_dispatch_token(*, user_id: int, agent_id: int, department=None) -> str:
    """Expired fixture via build_dispatch_claims — not issue_ overrides (W1 F4)."""
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    claims = build_dispatch_claims(
        user_id=user_id,
        department=department,
        agent_id=agent_id,
        issued_at=past,
        expires_at=past + timedelta(minutes=DISPATCH_TOKEN_TTL_MINUTES),
    )
    return jose_jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )


def _make_task(db, user, *, level: str = "無機密", trace_id: str | None = None) -> Task:
    kwargs = dict(
        title="測試任務",
        task_type="query",
        requester_user_id=user.id,
        status="submitted",
        classification_level=level,
    )
    if trace_id is not None:
        kwargs["trace_id"] = trace_id
    task = Task(**kwargs)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _service_client_token(db, name: str = "w2-poc5-sc") -> str:
    plaintext = generate_service_token()
    db.add(
        ServiceClient(
            client_name=name,
            client_type="router",
            service_token_envelope=encode_service_token_envelope(plaintext),
            service_token_lookup_hash=compute_lookup_hash(plaintext),
        )
    )
    db.commit()
    return plaintext


class _StubChunk:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubHit:
    def __init__(self, *, chunk, score, parent_content=None):
        self.chunk = chunk
        self.score = score
        self.parent_content = parent_content


class _StubStore:
    def __init__(self, hits):
        self._hits = hits

    async def similarity_search(self, *, query_embedding, top_k, min_score, **_kwargs):
        return self._hits


# ── verify unit ──────────────────────────────────────────────────────────────


def test_verify_dispatch_token_accepts_fresh_mint():
    token = issue_dispatch_token(user_id=7, department=3, agent_id=42)
    claims = verify_dispatch_token(token)
    assert claims is not None
    assert claims["user_id"] == 7
    assert claims["department"] == 3
    assert claims["agent_id"] == 42
    assert claims["iss"] == DISPATCH_TOKEN_ISSUER
    assert claims["aud"] == DISPATCH_TOKEN_AUDIENCE


def test_verify_dispatch_token_rejects_csk_prefix():
    assert verify_dispatch_token("csk-totally-not-a-jwt") is None


def test_verify_dispatch_token_rejects_missing_exp():
    """F5: python-jose skips exp when absent — verify must REQUIRE it."""
    from jose import jwt as jose_jwt

    from app.utils.security import ALGORITHM, get_private_key

    claims = {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "sub": "7",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        # deliberately no exp
    }
    token = jose_jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )
    assert "exp" not in jose_jwt.get_unverified_claims(token)
    assert verify_dispatch_token(token) is None


# ── (a) valid JWT authenticates search + bound scope ─────────────────────────


def test_a_valid_dispatch_jwt_search_scoped_to_bound_collections(
    client: TestClient, db, monkeypatch,
):
    owner = make_user(db, username="w2_owner_a")
    agent = make_agent(db, owner, name="w2-agent-a", approval_status="approved")
    bound = _collection(db, owner, "w2-bound-a")
    other = _collection(db, owner, "w2-unbound-a")
    set_bound_collection_ids(db, agent, [bound.id])
    db.commit()

    token = issue_dispatch_token(
        user_id=owner.id, department=owner.department_id, agent_id=agent.id
    )
    principal = _resolve(db, token)
    assert principal.agent is not None
    assert principal.agent.id == agent.id
    assert principal.user.id == owner.id  # owner mapping preserved for RLS
    _enforce_agent_collection_scope(principal, bound.id)
    with pytest.raises(HTTPException) as exc:
        _enforce_agent_collection_scope(principal, other.id)
    assert exc.value.status_code == 403

    # End-to-end: search endpoint accepts the JWT on the bound collection.
    async def fake_embed_query(db_, user, model_name, dim, query):
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed_query)
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())
    monkeypatch.setattr(
        search_mod,
        "CollectionScopedPgVectorStore",
        lambda pool, collection_id: _StubStore(
            [
                _StubHit(
                    chunk=_StubChunk(
                        id=1,
                        document_id=1,
                        chunk_key="chunk:1:001",
                        content="hit",
                        metadata={},
                        parent_chunk_id=None,
                        chunk_type="leaf",
                        chunk_level=0,
                    ),
                    score=0.9,
                )
            ]
        ),
    )
    resp = client.post(
        f"/api/ingestion/collections/{bound.id}/search",
        json={"query": "hello", "top_k": 3},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 1

    denied = client.post(
        f"/api/ingestion/collections/{other.id}/search",
        json={"query": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403


# ── (b) agent A JWT cannot search agent B's collections ──────────────────────


def test_b_agent_a_jwt_cannot_search_agent_b_collections(db):
    owner_a = make_user(db, username="w2_owner_a2")
    owner_b = make_user(db, username="w2_owner_b2")
    agent_a = make_agent(db, owner_a, name="w2-agent-a2", approval_status="approved")
    agent_b = make_agent(db, owner_b, name="w2-agent-b2", approval_status="approved")
    coll_b = _collection(db, owner_b, "w2-coll-b")
    set_bound_collection_ids(db, agent_a, [])
    set_bound_collection_ids(db, agent_b, [coll_b.id])
    db.commit()

    token_a = issue_dispatch_token(
        user_id=owner_a.id, department=None, agent_id=agent_a.id
    )
    principal = _resolve(db, token_a)
    with pytest.raises(HTTPException) as exc:
        _enforce_agent_collection_scope(principal, coll_b.id)
    assert exc.value.status_code == 403


# ── (c) expired JWT rejected ─────────────────────────────────────────────────


def test_c_expired_dispatch_jwt_rejected(db):
    owner = make_user(db, username="w2_owner_exp")
    agent = make_agent(db, owner, name="w2-agent-exp")
    token = _expired_dispatch_token(user_id=owner.id, agent_id=agent.id)
    assert verify_dispatch_token(token) is None
    with pytest.raises(HTTPException) as exc:
        _resolve(db, token)
    assert exc.value.status_code == 401


# ── (d) tampered JWT rejected ────────────────────────────────────────────────


def test_d_tampered_dispatch_jwt_rejected(db):
    owner = make_user(db, username="w2_owner_tamp")
    agent = make_agent(db, owner, name="w2-agent-tamp")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    tampered = _tamper_claim(token, "agent_id", agent.id + 999)
    assert verify_dispatch_token(tampered) is None
    with pytest.raises(HTTPException) as exc:
        _resolve(db, tampered)
    assert exc.value.status_code == 401


# ── (e) bare csk- rejected on converted paths ────────────────────────────────


def test_e_bare_csk_rejected_on_search(db):
    with pytest.raises(HTTPException) as exc:
        _resolve(db, "csk-dead-beef-not-accepted")
    assert exc.value.status_code == 401
    assert "派工 JWT" in exc.value.detail


def test_e_bare_agent_csk_rejected_on_artifacts(client: TestClient, db, monkeypatch):
    """Agent-kind csk- must not open /v1 artifact writes after W2."""
    from app.services import agent_credential_service

    owner = make_user(db, username="w2_art_owner")
    agent = make_agent(db, owner, name="w2-art-agent")
    cred, csk = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=owner, label="w2-test"
    )
    db.commit()
    assert csk.startswith("csk-")

    # Legacy fleet token disabled for this test so only the agent csk- is tried.
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifact-jobs",
        headers={"X-CSP-Service-Token": csk},
        json={
            "job_id": "w2-csk-reject",
            "artifact_type": "report",
            "requester_user_id": owner.id,
        },
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "服務權杖無效"
    _ = cred  # keep local for readability


def test_e_dispatch_jwt_rejected_on_artifacts(client: TestClient, db, monkeypatch):
    owner = make_user(db, username="w2_art_jwt_owner")
    agent = make_agent(db, owner, name="w2-art-jwt-agent")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "w2-jwt-accept",
            "artifact_type": "report",
            "requester_user_id": owner.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.get(ArtifactJob, "w2-jwt-accept") is None


def test_expired_dispatch_jwt_rejected_on_artifacts(client: TestClient, db, monkeypatch):
    owner = make_user(db, username="w2_art_exp")
    agent = make_agent(db, owner, name="w2-art-exp-agent")
    token = _expired_dispatch_token(user_id=owner.id, agent_id=agent.id)
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "w2-jwt-exp",
            "artifact_type": "report",
            "requester_user_id": owner.id,
        },
    )
    assert resp.status_code == 401, resp.text


def test_verify_dispatch_token_rejects_non_int_exp():
    """Defence in depth: exp=None/[]/{} must not TypeError → 500."""
    claims = {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "sub": "7",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": None,
    }
    token = jose_jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )
    assert verify_dispatch_token(token) is None


def test_service_client_legacy_still_works_on_artifacts(client: TestClient, monkeypatch):
    """service_clients / legacy fleet token path is out of W2 scope — keep."""
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "svc-w2-legacy-keep")
    resp = client.post(
        "/v1/artifact-jobs",
        headers={"X-CSP-Service-Token": "svc-w2-legacy-keep"},
        json={
            "job_id": "w2-legacy-ok",
            "artifact_type": "report",
            "requester_user_id": 1,
        },
    )
    assert resp.status_code == 201, resp.text


def test_access_token_still_cannot_create_artifacts(client: TestClient, db, monkeypatch):
    from app.utils.security import create_access_token

    user = make_user(db, username="w2_user_no_art")
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    access = create_access_token(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "tv": user.token_version,
        }
    )
    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {access}"},
        json={
            "job_id": "w2-user-deny",
            "artifact_type": "report",
            "requester_user_id": user.id,
        },
    )
    assert resp.status_code == 403, resp.text


# ── F3: agent-kind cannot name arbitrary requester_user_id ───────────────────


def test_f3_agent_cannot_set_arbitrary_requester_user_id(
    client: TestClient, db, monkeypatch,
):
    owner = make_user(db, username="w2_f3_owner")
    victim = make_user(db, username="w2_f3_victim")
    agent = make_agent(db, owner, name="w2-f3-agent")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "w2-f3-arb-req",
            "artifact_type": "report",
            "requester_user_id": victim.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.get(ArtifactJob, "w2-f3-arb-req") is None


def test_f3_agent_cannot_set_owner_via_employee_id(
    client: TestClient, db, monkeypatch,
):
    owner = make_user(db, username="w2_f3_emp_owner")
    victim = make_user(db, username="w2_f3_emp_victim")
    agent = make_agent(db, owner, name="w2-f3-emp-agent")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "w2-f3-emp",
            "artifact_type": "report",
            "requester_user_id": owner.id,
            "employee_id": victim.username,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.get(ArtifactJob, "w2-f3-emp") is None


def test_f3_agent_matching_requester_still_rejected(
    client: TestClient, db, monkeypatch,
):
    """即使 requester 與派工 JWT 的 user 相同，成品寫入也不接受這枚 token。"""
    owner = make_user(db, username="w2_f3_ok_owner")
    agent = make_agent(db, owner, name="w2-f3-ok-agent")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "w2-f3-ok",
            "artifact_type": "report",
            "requester_user_id": owner.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.get(ArtifactJob, "w2-f3-ok") is None


# ── F3 laundering PoC: foreign task/job/artifact must be rejected ────────────


def test_f3_agent_cannot_launder_victim_task_into_artifact(
    client: TestClient, db, monkeypatch,
):
    """Reviewer PoC: attacker agent + victim 機密 task → must REJECT.

    派工 JWT 不能在成品面寫入。被害人的任務欄位不該落到任何 Artifact。
    """
    victim = make_user(db, username="w2_f3_victim_task")
    attacker = make_user(db, username="w2_f3_attacker_task")
    agent = make_agent(db, attacker, name="w2-f3-attacker-agent")
    victim_task = _make_task(
        db, victim, level="機密", trace_id="victim-trace-001",
    )
    before = db.query(Artifact).count()
    token = issue_dispatch_token(
        user_id=attacker.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "artifact_type": "report",
            "title": "launder-poc",
            "storage_ref": "store://artifacts/launder.pdf",
            "task_id": victim_task.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.query(Artifact).count() == before
    leaked = (
        db.query(Artifact)
        .filter(
            (Artifact.owner_user_id == victim.id)
            | (Artifact.trace_id == "victim-trace-001")
        )
        .count()
    )
    assert leaked == 0


def test_f3_agent_own_task_cannot_register_artifact(
    client: TestClient, db, monkeypatch,
):
    owner = make_user(db, username="w2_f3_own_task")
    agent = make_agent(db, owner, name="w2-f3-own-agent")
    task = _make_task(db, owner, level="機密", trace_id="owner-trace-ok")
    before = db.query(Artifact).count()
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "artifact_type": "report",
            "title": "own-ok",
            "storage_ref": "store://artifacts/own.pdf",
            "task_id": task.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.query(Artifact).count() == before


def test_f3_agent_cannot_patch_foreign_job(
    client: TestClient, db, monkeypatch,
):
    victim = make_user(db, username="w2_f3_victim_job")
    attacker = make_user(db, username="w2_f3_attacker_job")
    agent = make_agent(db, attacker, name="w2-f3-patch-agent")
    # Seed victim-owned job via legacy service token, then attack with JWT.
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "svc-w2-f3-seed")
    seed = client.post(
        "/v1/artifact-jobs",
        headers={"X-CSP-Service-Token": "svc-w2-f3-seed"},
        json={
            "job_id": "w2-f3-victim-job",
            "artifact_type": "report",
            "requester_user_id": victim.id,
        },
    )
    assert seed.status_code == 201, seed.text
    assert seed.json()["owner_user_id"] == victim.id

    token = issue_dispatch_token(
        user_id=attacker.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    resp = client.patch(
        "/v1/artifact-jobs/w2-f3-victim-job",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "running"},
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    job = db.get(ArtifactJob, "w2-f3-victim-job")
    assert job is not None
    assert job.status == "queued"  # unchanged
    assert job.owner_user_id == victim.id


def test_f3_agent_cannot_version_foreign_artifact(
    client: TestClient, db, monkeypatch,
):
    victim = make_user(db, username="w2_f3_victim_ver")
    attacker = make_user(db, username="w2_f3_attacker_ver")
    agent = make_agent(db, attacker, name="w2-f3-ver-agent")
    victim_task = _make_task(db, victim, level="機密", trace_id="victim-ver-trace")

    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "svc-w2-f3-ver-seed")
    seed = client.post(
        "/v1/artifacts",
        headers={"X-CSP-Service-Token": "svc-w2-f3-ver-seed"},
        json={
            "artifact_type": "report",
            "title": "victim-art",
            "storage_ref": "store://artifacts/victim.pdf",
            "task_id": victim_task.id,
        },
    )
    assert seed.status_code == 201, seed.text
    art_id = seed.json()["artifact_id"]
    art = db.get(Artifact, art_id)
    assert art is not None
    assert art.owner_user_id == victim.id
    versions_before = len(art.versions) if hasattr(art, "versions") else None

    token = issue_dispatch_token(
        user_id=attacker.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    resp = client.post(
        f"/v1/artifacts/{art_id}/versions",
        headers={"Authorization": f"Bearer {token}"},
        json={"storage_ref": "store://artifacts/attacker-ver.pdf"},
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    db.refresh(art)
    assert art.owner_user_id == victim.id
    assert art.trace_id == "victim-ver-trace"
    if versions_before is not None:
        assert len(art.versions) == versions_before


def test_f3_agent_cannot_upsert_over_victim_job(
    client: TestClient, db, monkeypatch,
):
    """F-1: upsert on victim job_id must 403 and not echo victim fields."""
    victim = make_user(db, username="w2_f1_victim_upsert")
    attacker = make_user(db, username="w2_f1_attacker_upsert")
    agent = make_agent(db, attacker, name="w2-f1-upsert-agent")
    job = ArtifactJob(
        job_id="w2-f1-victim-job",
        owner_user_id=victim.id,
        artifact_type="report",
        status="running",
        progress=55,
        trace_id="victim-secret-trace",
        artifact_id=4242,
        result_metadata={"secret": "victim-only"},
        artifact_files=[{"path": "/victim/secret.pdf"}],
        message="victim message",
    )
    db.add(job)
    db.commit()

    token = issue_dispatch_token(
        user_id=attacker.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    resp = client.post(
        "/v1/artifact-jobs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "job_id": "w2-f1-victim-job",
            "artifact_type": "slides",
            "requester_user_id": attacker.id,
            "status": "queued",
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert "victim-only" not in resp.text
    assert "victim-secret-trace" not in resp.text
    assert "/victim/secret.pdf" not in resp.text
    db.expire_all()
    row = db.get(ArtifactJob, "w2-f1-victim-job")
    assert row is not None
    assert row.owner_user_id == victim.id
    assert row.status == "running"
    assert row.progress == 55
    assert row.trace_id == "victim-secret-trace"
    assert row.result_metadata == {"secret": "victim-only"}


# ── F-2: mutation-killing pins (snapshot / job scope + kid / iss+aud) ────────


def test_f3_agent_cannot_launder_victim_snapshot_into_artifact(
    client: TestClient, db, monkeypatch,
):
    """F-2 face1b: foreign source_snapshot_id must 403; no row lands."""
    victim = make_user(db, username="w2_f2_victim_snap")
    attacker = make_user(db, username="w2_f2_attacker_snap")
    agent = make_agent(db, attacker, name="w2-f2-snap-agent")
    victim_task = _make_task(
        db, victim, level="機密", trace_id="victim-snap-trace",
    )
    snap = SourceSnapshot(task_id=victim_task.id, classification_level="機密")
    db.add(snap)
    db.commit()
    db.refresh(snap)
    before = db.query(Artifact).count()
    token = issue_dispatch_token(
        user_id=attacker.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "artifact_type": "report",
            "title": "snap-launder",
            "storage_ref": "store://artifacts/snap.pdf",
            "source_snapshot_id": snap.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.query(Artifact).count() == before
    assert "機密" not in resp.text


def test_f3_agent_cannot_launder_victim_job_into_artifact(
    client: TestClient, db, monkeypatch,
):
    """F-2 face1c: foreign job_id on POST /v1/artifacts must 403.

    Own task_id is included so removing the job-scope guard would otherwise
    land a 201 (constitution §6 needs task/snapshot); the guard is what
    keeps the foreign job_id from attaching.
    """
    victim = make_user(db, username="w2_f2_victim_jobbind")
    attacker = make_user(db, username="w2_f2_attacker_jobbind")
    agent = make_agent(db, attacker, name="w2-f2-jobbind-agent")
    victim_task = _make_task(db, victim, trace_id="victim-job-trace")
    own_task = _make_task(db, attacker, level="無機密", trace_id="attacker-trace")
    job = ArtifactJob(
        job_id="w2-f2-victim-job-bind",
        owner_user_id=victim.id,
        artifact_type="report",
        status="queued",
        progress=0,
        trace_id="victim-job-trace",
        task_id=victim_task.id,
    )
    db.add(job)
    db.commit()
    before = db.query(Artifact).count()
    token = issue_dispatch_token(
        user_id=attacker.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")

    resp = client.post(
        "/v1/artifacts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "artifact_type": "report",
            "title": "job-launder",
            "storage_ref": "store://artifacts/job.pdf",
            "task_id": own_task.id,
            "job_id": "w2-f2-victim-job-bind",
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(resp)
    assert db.query(Artifact).count() == before


def test_verify_dispatch_token_rejects_wrong_and_absent_kid():
    """F-2: kid must resolve via _public_key_for_kid — bogus/absent → None."""
    claims = build_dispatch_claims(user_id=1, department=None, agent_id=1)
    pk = get_private_key()
    assert verify_dispatch_token(jose_jwt.encode(
        claims, pk, algorithm=ALGORITHM, headers={"kid": "bogus"},
    )) is None
    no_kid = jose_jwt.encode(claims, pk, algorithm=ALGORITHM)
    assert jose_jwt.get_unverified_header(no_kid).get("kid") is None
    assert verify_dispatch_token(no_kid) is None


def test_verify_dispatch_token_enforces_iss_and_aud():
    """F-2: iss/aud must be passed to jwt.decode — wrong values → None."""
    pk = get_private_key()
    base = build_dispatch_claims(user_id=1, department=None, agent_id=1)
    bad_iss = dict(base, iss="evil")
    bad_aud = dict(base, aud="anila-somethingelse")
    hdr = {"kid": settings.JWT_KID, "typ": "JWT"}
    assert verify_dispatch_token(jose_jwt.encode(
        bad_iss, pk, algorithm=ALGORITHM, headers=hdr,
    )) is None
    assert verify_dispatch_token(jose_jwt.encode(
        bad_aud, pk, algorithm=ALGORITHM, headers=hdr,
    )) is None


def test_f3_same_user_all_four_faces_reject_dispatch_jwt(
    client: TestClient, db, monkeypatch,
):
    """同一使用者的派工 JWT 在 job、patch、成品、版本四個面都被拒絕。"""
    owner = make_user(db, username="w2_f3_same_user")
    agent = make_agent(db, owner, name="w2-f3-same-agent")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}
    own = _make_task(db, owner, level="機密", trace_id="own-trace-ok")
    before = db.query(Artifact).count()

    r_job = client.post(
        "/v1/artifact-jobs",
        headers=headers,
        json={
            "job_id": "w2-same-own-job",
            "artifact_type": "report",
            "requester_user_id": owner.id,
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(r_job)
    assert db.get(ArtifactJob, "w2-same-own-job") is None

    r_patch = client.patch(
        "/v1/artifact-jobs/w2-same-own-job",
        headers=headers,
        json={"status": "running", "progress": 10},
    )
    _assert_dispatch_jwt_not_a_service_credential(r_patch)

    r_art = client.post(
        "/v1/artifacts",
        headers=headers,
        json={
            "artifact_type": "report",
            "title": "own",
            "storage_ref": "store://artifacts/own.pdf",
            "task_id": own.id,
            "job_id": "w2-same-own-job",
        },
    )
    _assert_dispatch_jwt_not_a_service_credential(r_art)
    assert db.query(Artifact).count() == before

    r_ver = client.post(
        "/v1/artifacts/1/versions",
        headers=headers,
        json={"storage_ref": "store://artifacts/own-v2.pdf"},
    )
    _assert_dispatch_jwt_not_a_service_credential(r_ver)


# ── F-4: agent missing-id must collapse to the same 403 as foreign-id ────────


def test_poc5_agent_missing_and_foreign_both_403(
    client: TestClient, db, monkeypatch,
):
    """派工 JWT 在查資源之前就被拒絕，缺漏與他人的 id 回同一句 401。"""
    owner = make_user(db, username="w2_f4_agent_owner")
    victim = make_user(db, username="w2_f4_agent_victim")
    agent = make_agent(db, owner, name="w2-f4-agent")
    token = issue_dispatch_token(
        user_id=owner.id, department=None, agent_id=agent.id
    )
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}

    victim_task = _make_task(
        db, victim, level="機密", trace_id="w2-f4-victim-trace",
    )
    db.add(
        ArtifactJob(
            job_id="w2-f4-victim-job",
            owner_user_id=victim.id,
            artifact_type="report",
            status="queued",
            progress=0,
            trace_id="w2-f4-victim-trace",
        )
    )
    victim_art = Artifact(
        artifact_type="report",
        title="w2-f4-victim-art",
        owner_user_id=victim.id,
        source_task_id=victim_task.id,
        trace_id="w2-f4-victim-trace",
        classification_level="機密",
    )
    db.add(victim_art)
    db.commit()
    db.refresh(victim_art)

    faces = [
        (
            "job",
            lambda: client.patch(
                "/v1/artifact-jobs/w2-f4-missing-job",
                headers=headers,
                json={"status": "running"},
            ),
            lambda: client.patch(
                "/v1/artifact-jobs/w2-f4-victim-job",
                headers=headers,
                json={"status": "running"},
            ),
        ),
        (
            "task",
            lambda: client.post(
                "/v1/artifacts",
                headers=headers,
                json={
                    "artifact_type": "report",
                    "title": "x",
                    "storage_ref": "store://x",
                    "task_id": 999997,
                },
            ),
            lambda: client.post(
                "/v1/artifacts",
                headers=headers,
                json={
                    "artifact_type": "report",
                    "title": "x",
                    "storage_ref": "store://x",
                    "task_id": victim_task.id,
                },
            ),
        ),
        (
            "artifact",
            lambda: client.post(
                "/v1/artifacts/999997/versions",
                headers=headers,
                json={"storage_ref": "store://x"},
            ),
            lambda: client.post(
                f"/v1/artifacts/{victim_art.id}/versions",
                headers=headers,
                json={"storage_ref": "store://x"},
            ),
        ),
    ]
    for label, missing_call, foreign_call in faces:
        missing = missing_call()
        foreign = foreign_call()
        _assert_dispatch_jwt_not_a_service_credential(missing)
        _assert_dispatch_jwt_not_a_service_credential(foreign)
        # 兩種 id 的回應必須相同，才不會洩漏資源是否存在。
        assert missing.json()["detail"] == foreign.json()["detail"], (
            label, missing.json()["detail"], foreign.json()["detail"]
        )


def test_poc5_legacy_token_missing_ids_still_404(
    client: TestClient, db, monkeypatch,
):
    """Contrast: non-agent legacy fleet token keeps genuine-missing 404s."""
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "w2-f4-legacy")
    headers = {"X-CSP-Service-Token": "w2-f4-legacy"}

    r = client.patch(
        "/v1/artifact-jobs/w2-f4-legacy-nope",
        headers=headers,
        json={"status": "running"},
    )
    assert r.status_code == 404, r.text

    r = client.post(
        "/v1/artifacts",
        headers=headers,
        json={
            "artifact_type": "report",
            "title": "x",
            "storage_ref": "store://x",
            "task_id": 999999,
        },
    )
    assert r.status_code == 404, r.text

    r = client.post(
        "/v1/artifacts",
        headers=headers,
        json={
            "artifact_type": "report",
            "title": "x",
            "storage_ref": "store://x",
            "source_snapshot_id": 999999,
        },
    )
    assert r.status_code == 404, r.text

    r = client.post(
        "/v1/artifacts/999999/versions",
        headers=headers,
        json={"storage_ref": "store://x"},
    )
    assert r.status_code == 404, r.text


def test_poc5_service_client_missing_ids_still_404(
    client: TestClient, db, monkeypatch,
):
    """Contrast: service_client path keeps genuine-missing 404s too."""
    tok = _service_client_token(db, name="w2-f4-sc")
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "")
    headers = {"X-CSP-Service-Token": tok}

    r = client.patch(
        "/v1/artifact-jobs/w2-f4-sc-nope",
        headers=headers,
        json={"status": "running"},
    )
    assert r.status_code == 404, r.text

    r = client.post(
        "/v1/artifacts",
        headers=headers,
        json={
            "artifact_type": "report",
            "title": "x",
            "storage_ref": "store://x",
            "task_id": 999998,
        },
    )
    assert r.status_code == 404, r.text

    r = client.post(
        "/v1/artifacts/999998/versions",
        headers=headers,
        json={"storage_ref": "store://x"},
    )
    assert r.status_code == 404, r.text
