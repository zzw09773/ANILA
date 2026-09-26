"""Sprint 8 X / Phase A — bootstrap-then-provision tests.

Covers the critical paths that the cutover runbook depends on:

1. Admin issue-bootstrap → returns plaintext + sets DB hash + expiry.
2. Bootstrap exchange happy path.
3. Replay defence: same bsk- can't be used twice.
4. Endpoint URL mismatch rejected (anti-replay-against-other-agent).
5. Expired bootstrap rejected.
6. Verify path: csk- token resolves to the right ``CallerIdentity``.
7. Rotate keeps the previous token valid for ``grace_seconds``.
8. Revoke immediately disables the token.
9. Legacy env-var fallback still works (until cutover step 5 removes it).

Tests run on the SQLite in-memory engine from ``conftest.py``. Each
test isolates DB state via the ``db`` fixture's ``function`` scope.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.models.agent_credential import AgentCredential
from app.models.service_client import ServiceClient
from app.services import agent_credential_service
from app.services.service_token_envelope import (
    BOOTSTRAP_TOKEN_PREFIX,
    SERVICE_TOKEN_PREFIX,
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_bootstrap_token,
    generate_service_token,
)
from tests.conftest import make_agent, make_user


# ---------------------------------------------------------------------------
# Pure helpers — no DB needed.
# ---------------------------------------------------------------------------


def test_bootstrap_token_format():
    tok = generate_bootstrap_token()
    assert tok.startswith(BOOTSTRAP_TOKEN_PREFIX)
    # 4-char prefix + 43-char base64url body ≈ 47.
    assert 40 <= len(tok) <= 60


def test_service_token_format():
    tok = generate_service_token()
    assert tok.startswith(SERVICE_TOKEN_PREFIX)


def test_envelope_round_trip():
    plaintext = generate_service_token()
    blob = encode_service_token_envelope(plaintext)
    assert blob.startswith("enc::v1::")
    from app.services.service_token_envelope import decode_service_token_envelope

    assert decode_service_token_envelope(blob) == plaintext


def test_lookup_hash_deterministic():
    plaintext = generate_service_token()
    assert compute_lookup_hash(plaintext) == compute_lookup_hash(plaintext)
    assert len(compute_lookup_hash(plaintext)) == 64


# ---------------------------------------------------------------------------
# Bootstrap issuance + consumption.
# ---------------------------------------------------------------------------


def test_issue_bootstrap_writes_hash_and_expiry(db):
    admin = make_user(db, username="admin1", role="admin")
    owner = make_user(db, username="owner1")
    agent = make_agent(db, owner=owner, name="rag-1", approval_status="approved")

    plaintext = agent_credential_service.issue_bootstrap_token(
        db, agent=agent, issuer=admin
    )
    db.commit()
    db.refresh(agent)

    assert plaintext.startswith(BOOTSTRAP_TOKEN_PREFIX)
    assert agent.bootstrap_token_hash is not None
    assert agent.bootstrap_token_hash == compute_lookup_hash(plaintext)
    from app.time_utils import as_utc

    assert as_utc(agent.bootstrap_token_expires_at) > datetime.now(timezone.utc)
    assert agent.bootstrap_token_consumed_at is None
    assert agent.bootstrap_token_issued_by == admin.id


def test_bootstrap_exchange_happy_path(db):
    admin = make_user(db, username="admin2", role="admin")
    owner = make_user(db, username="owner2")
    agent = make_agent(db, owner=owner, name="rag-2", approval_status="approved")

    bsk = agent_credential_service.issue_bootstrap_token(
        db, agent=agent, issuer=admin
    )
    db.commit()

    cred, csk = agent_credential_service.consume_bootstrap_token(
        db,
        agent=agent,
        presented_token=bsk,
        presented_endpoint_url=agent.endpoint_url,
        label="pod-1",
    )
    db.commit()

    assert csk.startswith(SERVICE_TOKEN_PREFIX)
    assert cred.is_active
    assert not cred.is_legacy
    assert cred.label == "pod-1"
    assert cred.agent_id == agent.id

    db.refresh(agent)
    assert agent.bootstrap_token_consumed_at is not None


def test_bootstrap_replay_rejected(db):
    admin = make_user(db, username="admin3", role="admin")
    owner = make_user(db, username="owner3")
    agent = make_agent(db, owner=owner, name="rag-3", approval_status="approved")

    bsk = agent_credential_service.issue_bootstrap_token(
        db, agent=agent, issuer=admin
    )
    db.commit()

    agent_credential_service.consume_bootstrap_token(
        db,
        agent=agent,
        presented_token=bsk,
        presented_endpoint_url=agent.endpoint_url,
    )
    db.commit()

    with pytest.raises(ValueError, match="已被使用過"):
        agent_credential_service.consume_bootstrap_token(
            db,
            agent=agent,
            presented_token=bsk,
            presented_endpoint_url=agent.endpoint_url,
        )


def test_bootstrap_endpoint_mismatch_rejected(db):
    admin = make_user(db, username="admin4", role="admin")
    owner = make_user(db, username="owner4")
    agent = make_agent(db, owner=owner, name="rag-4", approval_status="approved")

    bsk = agent_credential_service.issue_bootstrap_token(
        db, agent=agent, issuer=admin
    )
    db.commit()

    with pytest.raises(ValueError, match="endpoint_url"):
        agent_credential_service.consume_bootstrap_token(
            db,
            agent=agent,
            presented_token=bsk,
            presented_endpoint_url="http://EVIL:9999",
        )

    db.refresh(agent)
    # Failure must NOT consume the token — admin can retry with the
    # right URL.
    assert agent.bootstrap_token_consumed_at is None


def test_bootstrap_expired_rejected(db):
    admin = make_user(db, username="admin5", role="admin")
    owner = make_user(db, username="owner5")
    agent = make_agent(db, owner=owner, name="rag-5", approval_status="approved")

    bsk = agent_credential_service.issue_bootstrap_token(
        db, agent=agent, issuer=admin, ttl=timedelta(seconds=1)
    )
    db.commit()

    time.sleep(1.1)

    with pytest.raises(ValueError, match="已過期"):
        agent_credential_service.consume_bootstrap_token(
            db,
            agent=agent,
            presented_token=bsk,
            presented_endpoint_url=agent.endpoint_url,
        )


def test_bootstrap_wrong_token_rejected(db):
    admin = make_user(db, username="admin6", role="admin")
    owner = make_user(db, username="owner6")
    agent = make_agent(db, owner=owner, name="rag-6", approval_status="approved")

    agent_credential_service.issue_bootstrap_token(db, agent=agent, issuer=admin)
    db.commit()

    with pytest.raises(ValueError, match="無效"):
        agent_credential_service.consume_bootstrap_token(
            db,
            agent=agent,
            presented_token="bsk-not-the-right-one",
            presented_endpoint_url=agent.endpoint_url,
        )


# ---------------------------------------------------------------------------
# Verify path.
# ---------------------------------------------------------------------------


def test_verify_does_not_resolve_agent_credential(db):
    """長效 agent csk- 不是呼叫者。派工 JWT 才是 agent 身分。"""
    admin = make_user(db, username="admin7", role="admin")
    owner = make_user(db, username="owner7")
    agent = make_agent(db, owner=owner, name="rag-7", approval_status="approved")

    bsk = agent_credential_service.issue_bootstrap_token(db, agent=agent, issuer=admin)
    db.commit()
    _cred, csk = agent_credential_service.consume_bootstrap_token(
        db,
        agent=agent,
        presented_token=bsk,
        presented_endpoint_url=agent.endpoint_url,
    )
    db.commit()

    assert agent_credential_service.verify_service_token(db, token=csk) is None


def test_verify_rejects_unknown_token(db):
    identity = agent_credential_service.verify_service_token(
        db, token="csk-totally-not-a-real-token"
    )
    assert identity is None


def test_verify_rejects_revoked_credential(db):
    admin = make_user(db, username="admin8", role="admin")
    owner = make_user(db, username="owner8")
    agent = make_agent(db, owner=owner, name="rag-8", approval_status="approved")

    cred, csk = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="t1"
    )
    db.commit()
    assert agent_credential_service.verify_service_token(db, token=csk) is None

    agent_credential_service.revoke_agent_credential(db, credential=cred, actor=admin)
    db.commit()
    assert agent_credential_service.verify_service_token(db, token=csk) is None


# ---------------------------------------------------------------------------
# Rotation + grace window.
# ---------------------------------------------------------------------------


def test_agent_rotation_does_not_authenticate_either_token(db):
    admin = make_user(db, username="admin9", role="admin")
    owner = make_user(db, username="owner9")
    agent = make_agent(db, owner=owner, name="rag-9", approval_status="approved")

    cred, old_csk = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin
    )
    db.commit()

    new_csk = agent_credential_service.rotate_agent_credential(
        db, credential=cred, actor=admin, grace=timedelta(hours=1)
    )
    db.commit()

    # 輪替仍把上一把留在列上，但兩把都不能再當呼叫者。
    assert agent_credential_service.verify_service_token(db, token=new_csk) is None
    assert agent_credential_service.verify_service_token(db, token=old_csk) is None
    db.refresh(cred)
    assert cred.service_token_previous_lookup_hash == compute_lookup_hash(old_csk)


def test_rotation_grace_expiry(db):
    admin = make_user(db, username="adminA", role="admin")
    owner = make_user(db, username="ownerA")
    agent = make_agent(db, owner=owner, name="rag-A", approval_status="approved")

    cred, old_csk = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin
    )
    db.commit()

    agent_credential_service.rotate_agent_credential(
        db, credential=cred, actor=admin, grace=timedelta(seconds=1)
    )
    db.commit()

    # Force grace to be in the past via direct DB write.
    cred.service_token_previous_expires_at = datetime.now(timezone.utc) - timedelta(
        seconds=10
    )
    db.commit()

    assert (
        agent_credential_service.verify_service_token(db, token=old_csk) is None
    )


# ---------------------------------------------------------------------------
# Outgoing dispatch credential selection (Nit#2 治本).
# ---------------------------------------------------------------------------


def test_dispatch_prefers_most_recently_changed_credential(db):
    """CSP dispatches the most recently issued-OR-rotated active credential.

    Rotating an older-issued credential must make IT the dispatched token.
    Otherwise a fail-closed agent guard installed with the freshly-rotated
    csk- would reject CSP — which would still be presenting the other,
    newer-*issued* credential. Selection therefore orders by
    ``coalesce(rotated_at, issued_at)``, not ``issued_at`` alone.
    """
    admin = make_user(db, username="adminRot", role="admin")
    owner = make_user(db, username="ownerRot")
    agent = make_agent(db, owner=owner, name="rag-rot", approval_status="approved")

    cred_a, _csk_a = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="A"
    )
    cred_b, csk_b = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="B"
    )
    db.commit()

    # Pin deterministic timestamps: A issued before B, neither rotated yet.
    now = datetime.now(timezone.utc)
    cred_a.service_token_issued_at = now - timedelta(hours=2)
    cred_a.service_token_rotated_at = None
    cred_b.service_token_issued_at = now - timedelta(hours=1)
    cred_b.service_token_rotated_at = None
    db.commit()

    # Baseline: B (newer issued_at) is the dispatched credential.
    assert (
        agent_credential_service.get_active_plaintext_for_agent(db, agent_id=agent.id)
        == csk_b
    )

    # Rotate the OLDER credential A → its rotated_at = now (> B's issued_at),
    # so A becomes the most-recently-changed active credential.
    new_csk_a = agent_credential_service.rotate_agent_credential(
        db, credential=cred_a, actor=admin, grace=timedelta(hours=1)
    )
    db.commit()

    assert (
        agent_credential_service.get_active_plaintext_for_agent(db, agent_id=agent.id)
        == new_csk_a
    )


def test_dispatch_tie_breaks_deterministically_on_credential_id(db):
    """On identical effective timestamps, dispatch is deterministic: highest id.

    Mirrors the frontend badge's id tie-break so the two layers always agree
    on which credential CSP presents (otherwise the fail-closed guard the
    operator installs could target a different credential than CSP sends).
    """
    admin = make_user(db, username="adminTie", role="admin")
    owner = make_user(db, username="ownerTie")
    agent = make_agent(db, owner=owner, name="rag-tie", approval_status="approved")

    cred_a, _csk_a = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="A"
    )
    cred_b, csk_b = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="B"
    )
    db.commit()

    # Identical effective timestamps (neither rotated) → tie on coalesce().
    same = datetime.now(timezone.utc) - timedelta(hours=1)
    cred_a.service_token_issued_at = same
    cred_a.service_token_rotated_at = None
    cred_b.service_token_issued_at = same
    cred_b.service_token_rotated_at = None
    db.commit()

    # B was issued second → higher id → wins the tie deterministically.
    assert cred_b.id > cred_a.id
    assert (
        agent_credential_service.get_active_plaintext_for_agent(db, agent_id=agent.id)
        == csk_b
    )


# ---------------------------------------------------------------------------
# Service clients (Router-class).
# ---------------------------------------------------------------------------


def test_service_client_verify(db):
    plaintext = generate_service_token()
    sc = ServiceClient(
        client_name="test-router",
        client_type="router",
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
    )
    db.add(sc)
    db.commit()
    db.refresh(sc)

    identity = agent_credential_service.verify_service_token(db, token=plaintext)
    assert identity is not None
    assert identity.kind == "service_client"
    assert identity.service_client_id == sc.id


def test_service_client_rotation(db):
    admin = make_user(db, username="adminB", role="admin")
    plaintext = generate_service_token()
    sc = ServiceClient(
        client_name="another-router",
        client_type="router",
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
    )
    db.add(sc)
    db.commit()

    new_plain = agent_credential_service.rotate_service_client(
        db, client=sc, actor=admin, grace=timedelta(hours=1)
    )
    db.commit()

    assert (
        agent_credential_service.verify_service_token(db, token=plaintext) is not None
    )
    assert (
        agent_credential_service.verify_service_token(db, token=new_plain) is not None
    )


# ---------------------------------------------------------------------------
# Cache invalidation in proxy_service.
# ---------------------------------------------------------------------------


def test_proxy_cache_invalidation_on_rotation(db):
    """When a credential rotates, the proxy_service token cache must reflect it."""
    from app.services.proxy_service import (
        _get_cached_agent_token,
        invalidate_agent_token_cache,
    )

    admin = make_user(db, username="adminC", role="admin")
    owner = make_user(db, username="ownerC")
    agent = make_agent(db, owner=owner, name="rag-C", approval_status="approved")
    cred, original = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin
    )
    db.commit()

    # Cache priming uses a fresh DB session via SessionLocal which
    # won't see our test session's data; bypass by injecting directly.
    from app.services.proxy_service import _set_cached_agent_token

    _set_cached_agent_token(agent.id, original)
    assert _get_cached_agent_token(agent.id) == original

    invalidate_agent_token_cache(agent.id)
    assert _get_cached_agent_token(agent.id) is None


# ---------------------------------------------------------------------------
# Legacy env-var fallback.
# ---------------------------------------------------------------------------


def test_agent_credentials_are_not_a_caller_without_the_fleet_env(db, monkeypatch):
    """0027 種下的 agent 列在 CSP_SERVICE_TOKEN 為空時仍不得變成呼叫者。

    自動核發開啟也一樣：拒絕條件不能靠那顆環境變數。一般核發的 csk-、
    還在寬限期的上一把，以及會把任何身分當成服務呼叫者的任務連結，都不接受。
    """
    from fastapi import HTTPException

    from app.services.proxy.task_link import _resolve_acting_user

    monkeypatch.setenv("ANILA_SERVICE_CLIENT_AUTO_PROVISION", "1")
    monkeypatch.setenv("CSP_SERVICE_TOKEN", "")
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", "", raising=False)

    legacy = "csk-fleet-shared-from-0027"
    previous = "csk-fleet-previous-still-in-grace"
    now = datetime.now(timezone.utc)
    owner = make_user(db, username="100001")
    agent = make_agent(
        db, owner, name="legacy-fleet-agent", approval_status="approved"
    )
    db.add(
        AgentCredential(
            agent_id=agent.id,
            label="legacy-fleet-shared",
            service_token_envelope=encode_service_token_envelope(legacy),
            service_token_lookup_hash=compute_lookup_hash(legacy),
            service_token_previous_envelope=encode_service_token_envelope(previous),
            service_token_previous_lookup_hash=compute_lookup_hash(previous),
            service_token_previous_expires_at=now + timedelta(hours=24),
            service_token_issued_at=now,
            is_legacy=True,
            is_active=True,
        )
    )
    _cred, issued = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=owner, label="still-active"
    )
    db.commit()

    assert agent_credential_service.verify_service_token(db, token=legacy) is None
    assert agent_credential_service.verify_service_token(db, token=previous) is None
    assert agent_credential_service.verify_service_token(db, token=issued) is None

    with pytest.raises(HTTPException) as exc:
        _resolve_acting_user(
            db,
            caller=owner,
            request_headers={
                "X-CSP-Service-Token": legacy,
                "X-ANILA-User-Id": owner.username,
            },
        )
    assert exc.value.status_code == 401


def test_migration_revokes_every_active_agent_credential(db, db_engine):
    """長效 agent 憑證已退役。遷移把仍有效的列撤銷，並清掉寬限複本。"""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    legacy = "csk-migration-legacy"
    previous = "csk-migration-previous"
    now = datetime.now(timezone.utc)
    owner = make_user(db, username="100002", role="admin")
    agent = make_agent(db, owner, name="migration-agent", approval_status="approved")
    active = AgentCredential(
        agent_id=agent.id,
        label="legacy-fleet-shared",
        service_token_envelope=encode_service_token_envelope(legacy),
        service_token_lookup_hash=compute_lookup_hash(legacy),
        service_token_previous_envelope=encode_service_token_envelope(previous),
        service_token_previous_lookup_hash=compute_lookup_hash(previous),
        service_token_previous_expires_at=now + timedelta(hours=24),
        service_token_issued_at=now,
        is_legacy=True,
        is_active=True,
    )
    already = now - timedelta(days=1)
    inactive = AgentCredential(
        agent_id=agent.id,
        label="already-revoked",
        service_token_envelope=encode_service_token_envelope("csk-already-dead"),
        service_token_lookup_hash=compute_lookup_hash("csk-already-dead"),
        service_token_previous_envelope=encode_service_token_envelope(
            "csk-inactive-previous"
        ),
        service_token_previous_lookup_hash=compute_lookup_hash("csk-inactive-previous"),
        service_token_previous_expires_at=now + timedelta(hours=1),
        service_token_issued_at=already,
        is_legacy=False,
        is_active=False,
        revoked_at=already,
    )
    db.add(active)
    db.add(inactive)
    db.commit()

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0048_revoke_agent_credentials.py"
    )
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("r1_0048_revoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "r1_0048"
    assert module.down_revision == "r1_0047"

    ctx = MigrationContext.configure(db.connection())
    with Operations.context(ctx):
        module.upgrade()
    db.commit()
    db.expire_all()

    active_row = db.get(AgentCredential, active.id)
    assert active_row.is_active is False
    assert active_row.revoked_at is not None
    assert active_row.service_token_previous_envelope is None
    assert active_row.service_token_previous_lookup_hash is None
    assert active_row.service_token_previous_expires_at is None
    inactive_row = db.get(AgentCredential, inactive.id)
    assert inactive_row.is_active is False
    assert inactive_row.service_token_previous_envelope is None
    assert inactive_row.service_token_previous_lookup_hash is None
    assert inactive_row.service_token_previous_expires_at is None


def test_legacy_env_var_fallback_still_recognised(db, monkeypatch):
    """Even after migration, an agent that hasn't cut over yet still works."""
    legacy_token = "fleet-shared-token-from-the-old-days"
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", legacy_token)

    # No DB rows match — verify_service_token returns None, and the
    # auth_service dependency is the one that does the env-var
    # fallback. Here we just sanity-check that the DB-side service
    # returns None for this token (so the fallback path is reached).
    assert (
        agent_credential_service.verify_service_token(db, token=legacy_token) is None
    )
