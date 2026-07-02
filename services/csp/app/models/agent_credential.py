"""ORM model for ``agent_credentials`` — per-agent service tokens.

Sprint 8 X / Phase A. One ``Agent`` may own ``N`` credential rows so
that K8s multi-replica deployments can each bootstrap independently
without cross-pod coordination. Single-replica deployments leave the
1:N relationship at 1:1 with no overhead.

Schema notes
------------
* ``service_token_envelope`` — AES-256-GCM ``enc::v1::`` envelope. The
  authoritative ciphertext column. See
  ``app.services.service_token_envelope`` for encode / decode.
* ``service_token_lookup_hash`` — sha256(plaintext) hex string,
  deterministic, indexed. Lets the verify path find a candidate row in
  O(log n) before doing the constant-time compare against the
  decrypted envelope. Not a security boundary on its own.
* ``service_token_previous_*`` — overlap window when ``service_token``
  is rotated. The previous secret stays valid until
  ``service_token_previous_expires_at`` (default: rotation time + 24h).
  Long enough that an in-flight streaming SSE session does not get cut
  off and that CSP restarts have time to pick up the new value.
* ``client_cert_fingerprint`` — predeposit for future mTLS upgrade.
  NULL for this sprint; populated by a later migration once SPIRE /
  cert-manager is in place. Saving the column now means we will not
  need yet another migration to bolt mTLS on.
* ``is_legacy`` — set ``TRUE`` on rows backfilled from the old
  fleet-shared ``CSP_SERVICE_TOKEN`` env var. Rows with this flag
  surface in the dashboard as "needs cutover" so admins can pick them
  off one by one. New rows minted via ``POST /bootstrap`` /
  ``POST /credentials/issue-static`` always come back as ``FALSE``.
* ``label`` — free-text tag (e.g. ``"pod-1"``, ``"staging"``,
  ``"canary"``). Helps multi-replica deployments tell credentials
  apart in the admin UI. NULL for the default 1:1 case.
* ``revoked_at`` / ``revoked_by`` — soft-delete pattern. We never hard
  delete rows so the audit trail of "credential X was used between
  date A and date B" stays intact.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import backref, relationship

from app.database import Base


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    label = Column(String(100), nullable=True)

    # Authoritative ciphertext (enc::v1:: envelope). Decrypt via
    # ``decode_service_token_envelope``.
    service_token_envelope = Column(Text, nullable=False)
    # Indexed lookup key — sha256 hex of the same plaintext that lives
    # in ``service_token_envelope``. Updated atomically with the
    # envelope on rotation.
    service_token_lookup_hash = Column(String(64), nullable=False)

    # Previous-token overlap during rotation window (24h grace).
    service_token_previous_envelope = Column(Text, nullable=True)
    service_token_previous_lookup_hash = Column(String(64), nullable=True)
    service_token_previous_expires_at = Column(DateTime, nullable=True)

    service_token_issued_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    service_token_rotated_at = Column(DateTime, nullable=True)

    # mTLS pre-deposit (Sprint 8 X / decision #7). Always NULL for now.
    client_cert_fingerprint = Column(String(128), nullable=True)

    # Backfilled-from-shared-env-var rows are flagged so admins can
    # audit cutover progress.
    is_legacy = Column(Boolean, nullable=False, default=False, server_default="false")

    # Soft delete. ``is_active=False`` means the verify path skips this
    # row even though it stays around for audit.
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # passive_deletes=True: trust the DB-level CASCADE on agent_credentials.
    # agent_id (NOT NULL). Without it, SQLAlchemy tries to NULL out child
    # agent_id on Agent delete and fails the NOT NULL check before the
    # CASCADE can fire — manifests as 500 on DELETE /api/agents/{id}
    # whenever the agent has at least one credential row.
    agent = relationship(
        "Agent",
        backref=backref("credentials", passive_deletes=True),
    )
    revoker = relationship("User", foreign_keys=[revoked_by])

    __table_args__ = (
        # Verify path: WHERE service_token_lookup_hash = $1 AND is_active.
        # Partial index keeps the active set tight.
        Index(
            "idx_agent_credentials_active_hash",
            "service_token_lookup_hash",
            postgresql_where=(is_active.is_(True)),
        ),
        # Same for previous-token grace lookup. Sparse — most rows have
        # NULL here — so a partial index keeps the index tiny.
        Index(
            "idx_agent_credentials_active_prev_hash",
            "service_token_previous_lookup_hash",
            postgresql_where=(
                (is_active.is_(True))
                & (service_token_previous_lookup_hash.isnot(None))
            ),
        ),
    )
