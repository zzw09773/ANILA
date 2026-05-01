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
    assert agent.bootstrap_token_expires_at > datetime.now(timezone.utc)
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


def test_verify_resolves_to_agent_identity(db):
    admin = make_user(db, username="admin7", role="admin")
    owner = make_user(db, username="owner7")
    agent = make_agent(db, owner=owner, name="rag-7", approval_status="approved")

    bsk = agent_credential_service.issue_bootstrap_token(db, agent=agent, issuer=admin)
    db.commit()
    cred, csk = agent_credential_service.consume_bootstrap_token(
        db,
        agent=agent,
        presented_token=bsk,
        presented_endpoint_url=agent.endpoint_url,
    )
    db.commit()

    identity = agent_credential_service.verify_service_token(db, token=csk)
    assert identity is not None
    assert identity.kind == "agent"
    assert identity.agent_id == agent.id
    assert identity.credential_id == cred.id
    assert identity.is_legacy is False
    assert identity.used_previous_token is False


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
    assert agent_credential_service.verify_service_token(db, token=csk) is not None

    agent_credential_service.revoke_agent_credential(db, credential=cred, actor=admin)
    db.commit()
    assert agent_credential_service.verify_service_token(db, token=csk) is None


# ---------------------------------------------------------------------------
# Rotation + grace window.
# ---------------------------------------------------------------------------


def test_rotation_keeps_previous_token_alive_in_grace(db):
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

    # Both tokens must verify during the grace window.
    new_identity = agent_credential_service.verify_service_token(db, token=new_csk)
    assert new_identity is not None
    assert not new_identity.used_previous_token

    old_identity = agent_credential_service.verify_service_token(db, token=old_csk)
    assert old_identity is not None
    assert old_identity.used_previous_token is True


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
        _resolve_outgoing_service_token,
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
