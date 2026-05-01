"""Issue / verify / rotate / revoke per-agent and service-client tokens.

Sprint 8 X / Phase A. The single place that knows how the
``agent_credentials`` and ``service_clients`` tables are written. All
HTTP endpoints and middleware go through here, so future schema
tweaks (e.g. adding mTLS fingerprint enforcement, moving rotation to
async tasks) only have to land in one file.

Verification model
==================

Both tables share the same wire protocol (``X-CSP-Service-Token``
header) and the same lookup-hash + envelope storage. The verify path
is therefore identical apart from the table name; the public
``verify_service_token`` returns a ``CallerIdentity`` describing which
table matched and the row id, which the rest of the platform uses for
audit / attribution / blast-radius limiting.

Caller identity priority
========================

If two rows happen to share the same plaintext (e.g. an admin issued
the same token twice — should not happen with 256-bit entropy but the
code defends anyway), we resolve in this order:

  1. ``service_clients`` (Router / worker)
  2. ``agent_credentials`` (per-agent)
  3. Legacy env-var fallback (proxy_service still consults env when
     the DB has nothing — handled by ``verify_service_token_legacy``)

This matches the deployment-time invariant that Router boots before
agents register; if an admin ever flips the order, they need to be
explicit about it.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.agent_credential import AgentCredential
from app.models.service_client import ServiceClient
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.service_token_envelope import (
    compute_lookup_hash,
    decode_service_token_envelope,
    encode_service_token_envelope,
    generate_bootstrap_token,
    generate_service_token,
    hash_bootstrap_token,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit event constants. Defined here (not in audit_service) because
# only this module emits them — keeps the vocabulary close to the code
# that writes it.
# ---------------------------------------------------------------------------
AUDIT_BOOTSTRAP_ISSUED = "service_token_bootstrap_issued"
AUDIT_BOOTSTRAP_CONSUMED = "service_token_bootstrap_consumed"
AUDIT_TOKEN_ISSUED = "service_token_issued"
AUDIT_TOKEN_ROTATED = "service_token_rotated"
AUDIT_TOKEN_REVOKED = "service_token_revoked"
AUDIT_TOKEN_VERIFIED = "service_token_verified"
AUDIT_LEGACY_TOKEN_USED = "service_token_legacy_env_used"


# Default TTLs. The bootstrap window is short on purpose — the admin
# hands the token to the agent operator, who pastes it into the
# agent's env / state file, and the agent boots within minutes. A
# longer window just widens the steal-and-replay surface.
BOOTSTRAP_DEFAULT_TTL = timedelta(minutes=15)
ROTATION_GRACE_DEFAULT = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Identity resolved from a ``X-CSP-Service-Token`` header.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CallerIdentity:
    """Result of a successful ``verify_service_token``.

    ``kind`` is ``"agent"`` or ``"service_client"``. Exactly one of
    ``agent_id`` / ``service_client_id`` is populated; the other is
    ``None``. ``credential_id`` is the row id within whichever table
    matched, used for audit / metric attribution.
    """

    kind: str
    agent_id: Optional[int]
    service_client_id: Optional[int]
    credential_id: int
    is_legacy: bool
    used_previous_token: bool


# ---------------------------------------------------------------------------
# Verification path
# ---------------------------------------------------------------------------


def verify_service_token(
    db: Session,
    *,
    token: str,
) -> Optional[CallerIdentity]:
    """Resolve a presented service token to a caller identity.

    Returns ``None`` if no match. Caller (middleware /
    ``verify_service_token`` dependency) is responsible for translating
    ``None`` into HTTP 401.

    The lookup hash is the indexed key; the constant-time
    ``hmac.compare_digest`` against the decrypted envelope is the
    actual security check.
    """
    if not token:
        return None

    lookup_hash = compute_lookup_hash(token)
    now = datetime.now(timezone.utc)

    # 1) service_clients first (Router / worker traffic dominates s2s
    #    volume; checking it first reduces average latency).
    sc_match = _match_service_client(db, token=token, lookup_hash=lookup_hash, now=now)
    if sc_match is not None:
        return sc_match

    # 2) agent_credentials.
    ac_match = _match_agent_credential(db, token=token, lookup_hash=lookup_hash, now=now)
    if ac_match is not None:
        return ac_match

    return None


def _match_service_client(
    db: Session, *, token: str, lookup_hash: str, now: datetime
) -> Optional[CallerIdentity]:
    candidates = (
        db.query(ServiceClient)
        .filter(
            ServiceClient.is_active.is_(True),
            or_(
                ServiceClient.service_token_lookup_hash == lookup_hash,
                and_(
                    ServiceClient.service_token_previous_lookup_hash == lookup_hash,
                    ServiceClient.service_token_previous_expires_at > now,
                ),
            ),
        )
        .all()
    )
    for client in candidates:
        used_previous = False
        try:
            primary_pt = decode_service_token_envelope(client.service_token_envelope)
            if primary_pt and hmac.compare_digest(primary_pt, token):
                return CallerIdentity(
                    kind="service_client",
                    agent_id=None,
                    service_client_id=client.id,
                    credential_id=client.id,
                    is_legacy=client.is_legacy,
                    used_previous_token=False,
                )
            previous_pt = decode_service_token_envelope(
                client.service_token_previous_envelope
            )
            if (
                previous_pt
                and client.service_token_previous_expires_at
                and client.service_token_previous_expires_at > now
                and hmac.compare_digest(previous_pt, token)
            ):
                used_previous = True
                return CallerIdentity(
                    kind="service_client",
                    agent_id=None,
                    service_client_id=client.id,
                    credential_id=client.id,
                    is_legacy=client.is_legacy,
                    used_previous_token=True,
                )
        except Exception:  # noqa: BLE001 — corrupt envelope, log and skip
            logger.exception(
                "Failed to decrypt service_clients row id=%s during verify; skipping",
                client.id,
            )
            continue
    return None


def _match_agent_credential(
    db: Session, *, token: str, lookup_hash: str, now: datetime
) -> Optional[CallerIdentity]:
    candidates = (
        db.query(AgentCredential)
        .filter(
            AgentCredential.is_active.is_(True),
            or_(
                AgentCredential.service_token_lookup_hash == lookup_hash,
                and_(
                    AgentCredential.service_token_previous_lookup_hash == lookup_hash,
                    AgentCredential.service_token_previous_expires_at > now,
                ),
            ),
        )
        .all()
    )
    for cred in candidates:
        try:
            primary_pt = decode_service_token_envelope(cred.service_token_envelope)
            if primary_pt and hmac.compare_digest(primary_pt, token):
                return CallerIdentity(
                    kind="agent",
                    agent_id=cred.agent_id,
                    service_client_id=None,
                    credential_id=cred.id,
                    is_legacy=cred.is_legacy,
                    used_previous_token=False,
                )
            previous_pt = decode_service_token_envelope(
                cred.service_token_previous_envelope
            )
            if (
                previous_pt
                and cred.service_token_previous_expires_at
                and cred.service_token_previous_expires_at > now
                and hmac.compare_digest(previous_pt, token)
            ):
                return CallerIdentity(
                    kind="agent",
                    agent_id=cred.agent_id,
                    service_client_id=None,
                    credential_id=cred.id,
                    is_legacy=cred.is_legacy,
                    used_previous_token=True,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to decrypt agent_credentials row id=%s during verify; skipping",
                cred.id,
            )
            continue
    return None


# ---------------------------------------------------------------------------
# Bootstrap token issuance (admin-driven)
# ---------------------------------------------------------------------------


def issue_bootstrap_token(
    db: Session,
    *,
    agent: Agent,
    issuer: User,
    ttl: timedelta = BOOTSTRAP_DEFAULT_TTL,
) -> str:
    """Mint a single-use ``bsk-`` token for one agent.

    Stores only the sha256 hash on ``agents``; the plaintext is
    returned exactly once. Re-issuing while the previous bootstrap is
    still valid replaces it (the previous bsk- becomes immediately
    unusable — admin intent is "I want to re-issue").
    """
    plaintext = generate_bootstrap_token()
    now = datetime.now(timezone.utc)
    agent.bootstrap_token_hash = hash_bootstrap_token(plaintext)
    agent.bootstrap_token_expires_at = now + ttl
    agent.bootstrap_token_consumed_at = None
    agent.bootstrap_token_issued_by = issuer.id
    db.flush()
    log_audit_event(
        db,
        actor=issuer,
        action=AUDIT_BOOTSTRAP_ISSUED,
        resource_type="agent",
        resource_id=agent.id,
        detail=f"bootstrap token issued, ttl={int(ttl.total_seconds())}s",
        metadata={"agent_name": agent.name, "ttl_seconds": int(ttl.total_seconds())},
    )
    return plaintext


def consume_bootstrap_token(
    db: Session,
    *,
    agent: Agent,
    presented_token: str,
    presented_endpoint_url: str,
    label: Optional[str] = None,
) -> tuple[AgentCredential, str]:
    """Atomic single-use exchange of a ``bsk-`` token for a service token.

    On success returns ``(credential_row, plaintext_csk_token)``. On
    any failure raises ``ValueError`` with a translatable message.

    The atomicity is enforced via a conditional UPDATE — only the row
    where ``bootstrap_token_consumed_at IS NULL`` matches, so two
    racing calls see exactly one winner. Validation rules:

      * Bootstrap hash must match.
      * Expiry must be in the future.
      * ``consumed_at`` must be NULL (single-use).
      * Caller-supplied ``endpoint_url`` must match the registered
        agent's ``endpoint_url`` — guards against a leaked bsk- being
        replayed against a different agent.
    """
    if not presented_token:
        raise ValueError("缺少 bootstrap token")

    if not agent.bootstrap_token_hash:
        raise ValueError("此 agent 沒有可用的 bootstrap token，請請 admin 重新核發")

    presented_hash = hash_bootstrap_token(presented_token)
    if not hmac.compare_digest(presented_hash, agent.bootstrap_token_hash):
        raise ValueError("bootstrap token 無效")

    now = datetime.now(timezone.utc)
    if not agent.bootstrap_token_expires_at or agent.bootstrap_token_expires_at <= now:
        raise ValueError("bootstrap token 已過期，請請 admin 重新核發")

    if agent.bootstrap_token_consumed_at is not None:
        raise ValueError("bootstrap token 已被使用過")

    if (
        not presented_endpoint_url
        or presented_endpoint_url.strip() != (agent.endpoint_url or "").strip()
    ):
        raise ValueError(
            "endpoint_url 不符 agent 的註冊資料；請確認 agent_id 與 endpoint_url 配對正確"
        )

    # Atomic CAS: stamp ``consumed_at`` only when it is currently NULL.
    # If two replicas race, exactly one UPDATE returns rowcount=1.
    result = db.execute(
        Agent.__table__.update()
        .where(Agent.id == agent.id)
        .where(Agent.bootstrap_token_consumed_at.is_(None))
        .values(bootstrap_token_consumed_at=now)
    )
    if result.rowcount != 1:
        raise ValueError("bootstrap token 已被使用過（race detected）")

    plaintext = generate_service_token()
    credential = AgentCredential(
        agent_id=agent.id,
        label=label,
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
        service_token_issued_at=now,
        is_legacy=False,
        is_active=True,
    )
    db.add(credential)
    db.flush()  # populate credential.id

    log_audit_event(
        db,
        actor=None,
        action=AUDIT_BOOTSTRAP_CONSUMED,
        resource_type="agent_credential",
        resource_id=credential.id,
        detail=f"bootstrap consumed → credential issued for agent={agent.id}",
        metadata={
            "agent_id": agent.id,
            "agent_name": agent.name,
            "label": label,
        },
    )
    log_audit_event(
        db,
        actor=None,
        action=AUDIT_TOKEN_ISSUED,
        resource_type="agent_credential",
        resource_id=credential.id,
        detail="service token issued via bootstrap exchange",
    )
    return credential, plaintext


# ---------------------------------------------------------------------------
# Static issuance (admin-driven, no bootstrap exchange)
# ---------------------------------------------------------------------------


def issue_static_credential(
    db: Session,
    *,
    agent: Agent,
    issuer: User,
    label: Optional[str] = None,
) -> tuple[AgentCredential, str]:
    """Phase F — admin authority, skip bootstrap, mint a credential directly.

    For agents that cannot run the ``anila-core agent bootstrap`` CLI
    (third-party / non-Python / Tier 0 cutover from legacy fleet-shared
    token). Admin pastes the returned plaintext into the agent's env
    var. No automatic rotation — admin must rotate manually every N
    days; the dashboard surfaces overdue rows.
    """
    plaintext = generate_service_token()
    now = datetime.now(timezone.utc)
    credential = AgentCredential(
        agent_id=agent.id,
        label=label,
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
        service_token_issued_at=now,
        is_legacy=False,
        is_active=True,
    )
    db.add(credential)
    db.flush()
    log_audit_event(
        db,
        actor=issuer,
        action=AUDIT_TOKEN_ISSUED,
        resource_type="agent_credential",
        resource_id=credential.id,
        detail="static credential issued by admin (skipped bootstrap)",
        metadata={"agent_id": agent.id, "agent_name": agent.name, "label": label},
    )
    return credential, plaintext


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def rotate_agent_credential(
    db: Session,
    *,
    credential: AgentCredential,
    actor: Optional[User] = None,
    grace: timedelta = ROTATION_GRACE_DEFAULT,
) -> str:
    """Issue a new plaintext for an existing credential row.

    The previous envelope is moved into ``service_token_previous_*``
    and stays valid until ``service_token_previous_expires_at``
    (default ``+24h``). This overlap window keeps long-lived SSE
    sessions alive across rotation and gives CSP / agents room to
    pick up the new token without coordinated restart.
    """
    plaintext = generate_service_token()
    now = datetime.now(timezone.utc)

    credential.service_token_previous_envelope = credential.service_token_envelope
    credential.service_token_previous_lookup_hash = credential.service_token_lookup_hash
    credential.service_token_previous_expires_at = now + grace

    credential.service_token_envelope = encode_service_token_envelope(plaintext)
    credential.service_token_lookup_hash = compute_lookup_hash(plaintext)
    credential.service_token_rotated_at = now
    credential.is_legacy = False  # rotated rows are no longer "fleet-shared"

    db.flush()
    log_audit_event(
        db,
        actor=actor,
        action=AUDIT_TOKEN_ROTATED,
        resource_type="agent_credential",
        resource_id=credential.id,
        detail=f"credential rotated (grace={int(grace.total_seconds())}s)",
        metadata={
            "agent_id": credential.agent_id,
            "grace_seconds": int(grace.total_seconds()),
        },
    )
    return plaintext


def rotate_service_client(
    db: Session,
    *,
    client: ServiceClient,
    actor: Optional[User] = None,
    grace: timedelta = ROTATION_GRACE_DEFAULT,
) -> str:
    plaintext = generate_service_token()
    now = datetime.now(timezone.utc)

    client.service_token_previous_envelope = client.service_token_envelope
    client.service_token_previous_lookup_hash = client.service_token_lookup_hash
    client.service_token_previous_expires_at = now + grace

    client.service_token_envelope = encode_service_token_envelope(plaintext)
    client.service_token_lookup_hash = compute_lookup_hash(plaintext)
    client.service_token_rotated_at = now
    client.is_legacy = False

    db.flush()
    log_audit_event(
        db,
        actor=actor,
        action=AUDIT_TOKEN_ROTATED,
        resource_type="service_client",
        resource_id=client.id,
        detail=f"service_client rotated (grace={int(grace.total_seconds())}s)",
        metadata={"client_name": client.client_name},
    )
    return plaintext


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def revoke_agent_credential(
    db: Session,
    *,
    credential: AgentCredential,
    actor: Optional[User] = None,
    reason: Optional[str] = None,
) -> None:
    if not credential.is_active:
        return  # idempotent
    now = datetime.now(timezone.utc)
    credential.is_active = False
    credential.revoked_at = now
    credential.revoked_by = actor.id if actor else None
    db.flush()
    log_audit_event(
        db,
        actor=actor,
        action=AUDIT_TOKEN_REVOKED,
        resource_type="agent_credential",
        resource_id=credential.id,
        detail=reason or "credential revoked",
        metadata={"agent_id": credential.agent_id},
    )


def revoke_service_client(
    db: Session,
    *,
    client: ServiceClient,
    actor: Optional[User] = None,
    reason: Optional[str] = None,
) -> None:
    if not client.is_active:
        return
    now = datetime.now(timezone.utc)
    client.is_active = False
    client.revoked_at = now
    client.revoked_by = actor.id if actor else None
    db.flush()
    log_audit_event(
        db,
        actor=actor,
        action=AUDIT_TOKEN_REVOKED,
        resource_type="service_client",
        resource_id=client.id,
        detail=reason or "service_client revoked",
        metadata={"client_name": client.client_name},
    )


# ---------------------------------------------------------------------------
# Plaintext fetch for outgoing s2s headers (used by proxy_service)
# ---------------------------------------------------------------------------


def get_active_plaintext_for_agent(
    db: Session, *, agent_id: int
) -> Optional[str]:
    """Return the plaintext token for the most recently issued active
    credential of an agent, or ``None`` if no active credential exists.

    Multi-replica deployments may have several active credentials per
    agent — pick the most recent one. Outgoing CSP→agent calls only
    need ONE valid token; the agent verifies it on its side.
    """
    cred = (
        db.query(AgentCredential)
        .filter(
            AgentCredential.agent_id == agent_id,
            AgentCredential.is_active.is_(True),
        )
        .order_by(AgentCredential.service_token_issued_at.desc())
        .first()
    )
    if cred is None:
        return None
    try:
        return decode_service_token_envelope(cred.service_token_envelope)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to decrypt agent_credentials id=%s for outgoing call", cred.id
        )
        return None


__all__ = [
    "AUDIT_BOOTSTRAP_CONSUMED",
    "AUDIT_BOOTSTRAP_ISSUED",
    "AUDIT_LEGACY_TOKEN_USED",
    "AUDIT_TOKEN_ISSUED",
    "AUDIT_TOKEN_REVOKED",
    "AUDIT_TOKEN_ROTATED",
    "AUDIT_TOKEN_VERIFIED",
    "BOOTSTRAP_DEFAULT_TTL",
    "CallerIdentity",
    "ROTATION_GRACE_DEFAULT",
    "consume_bootstrap_token",
    "get_active_plaintext_for_agent",
    "issue_bootstrap_token",
    "issue_static_credential",
    "revoke_agent_credential",
    "revoke_service_client",
    "rotate_agent_credential",
    "rotate_service_client",
    "verify_service_token",
]
