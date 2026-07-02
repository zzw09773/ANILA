"""ORM model for ``service_clients`` — non-agent service-to-service callers.

Sprint 8 X / Phase A. Used for callers that are not registered as
``agents`` (Router, ingestion-worker, future admin tooling). Keeping
them out of the ``agents`` table avoids forcing the agent-only
constraints (``base_model_id`` NOT NULL, approval workflow,
``/v1/agents`` manifest list) onto things that are not agents.

Same encryption / lookup pattern as ``agent_credentials`` so a single
helper module (``service_token_envelope``) covers both.

``client_type`` taxonomy
------------------------
* ``router``        — anila-core-router itself. Today exactly one row,
                      ``client_name='router-primary'``, used by Router
                      to call CSP's ``/api/models/router-primary`` and
                      anywhere else CSP-internal s2s is needed.
* ``worker``        — async pipeline workers. ingestion-worker is the
                      first candidate for Sprint 9 X; this column lets
                      us distinguish its traffic from Router traffic
                      in usage / audit dashboards.
* ``admin_tool``    — future surface for ops scripts that need to call
                      CSP s2s endpoints without holding admin JWT
                      (e.g. backup helpers, on-call runbooks).

Multi-replica
-------------
Unlike ``agent_credentials`` we keep ``service_clients`` 1:1 with a
client name — the row IS the client. Multiple replicas of the same
service share the row's plaintext token; rotation pushes a new token
to all replicas via the state-file refresh path (Phase B / Phase C).
We can revisit if a service ever needs per-replica tokens.
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
from sqlalchemy.orm import relationship

from app.database import Base


class ServiceClient(Base):
    __tablename__ = "service_clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_name = Column(String(100), nullable=False, unique=True, index=True)
    client_type = Column(String(20), nullable=False)
    description = Column(String(500), nullable=True)

    # Authoritative ciphertext + indexed lookup hash. Same shape as
    # ``agent_credentials``.
    service_token_envelope = Column(Text, nullable=False)
    service_token_lookup_hash = Column(String(64), nullable=False, unique=True)

    service_token_previous_envelope = Column(Text, nullable=True)
    service_token_previous_lookup_hash = Column(String(64), nullable=True)
    service_token_previous_expires_at = Column(DateTime, nullable=True)

    service_token_issued_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    service_token_rotated_at = Column(DateTime, nullable=True)

    client_cert_fingerprint = Column(String(128), nullable=True)

    is_legacy = Column(Boolean, nullable=False, default=False, server_default="false")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    revoker = relationship("User", foreign_keys=[revoked_by])

    __table_args__ = (
        Index(
            "idx_service_clients_active_prev_hash",
            "service_token_previous_lookup_hash",
            postgresql_where=(
                (is_active.is_(True))
                & (service_token_previous_lookup_hash.isnot(None))
            ),
        ),
    )
