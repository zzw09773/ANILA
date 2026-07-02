# -*- coding: utf-8 -*-
"""RegisteredService — the Slice 7 additive upgrade of ``platform_links``.

doc 07 §3 defines ``RegisteredService`` as a superset of the legacy
``platform_links`` row: the same nine visible-link fields plus 14 new
target fields (slug, owner department / admin, service_admin list, service
type, project entry, origin allow-list, launch mode, iframe flag, sso mode,
launch-token support, health / audit / trace callback URLs, classification
ceiling) and the v0.2 ``config_source`` source-of-truth trio (config_source,
env_seed_key, db_editable_fields, last_seeded_at).

The table is the single source of truth for the Service Registry; the legacy
``platform_links`` table is KEPT intact (downgrade safety, see
``models/platform_link.py`` docstring) but is no longer written to by the
seed or the ``/api/platform-links`` compat façade — both now operate on
``registered_services``.

``sort_order`` is carried over from ``platform_links`` even though doc §3's
schema block omits it, because §2 / §14 say to keep the existing link fields
and the doc's own ``db_editable_fields`` example references ``sort_order``.
It powers the compat ``PlatformLinkResponse`` shape and list ordering.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.database import Base

# Postgres → JSONB (indexable); SQLite pytest fixtures → plain JSON. Same
# pattern as ``platform_link._REQUIRED_ROLES_TYPE``.
_JSON_LIST = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegisteredService(Base):
    __tablename__ = "registered_services"

    # ── doc §3 schema (33 fields) + carried-over sort_order ──────────────────
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    # ``slug`` is the stable string service identifier used as the launch
    # token ``aud`` / ``service_id`` claim and as the {service_id} path param.
    slug = Column(String(120), nullable=False, unique=True, index=True)
    description = Column(String(255), nullable=True)
    icon = Column(String(50), nullable=True)

    # doc §3 types owner_department_id / owner_admin_user_id as required, but
    # legacy platform_links carry neither — nullable so the data-migration of
    # existing rows can't invent an owner. New rows may enforce at the API.
    owner_department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    owner_admin_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # per-service delegation list, NOT a global role (doc §3 note / §11).
    service_admin_user_ids = Column(_JSON_LIST, nullable=False, default=list)

    service_type = Column(
        String(30), nullable=False, server_default="project_portal"
    )
    project_entry = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    project_id = Column(String(100), nullable=True)

    entry_url = Column(String(500), nullable=False)
    allowed_origins = Column(_JSON_LIST, nullable=False, default=list)
    launch_mode = Column(String(20), nullable=False, server_default="new_tab")
    iframe_allowed = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    sso_mode = Column(String(20), nullable=False, server_default="card_sso")
    supports_launch_token = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    data_ownership = Column(
        String(20), nullable=False, server_default="self_managed"
    )
    data_ingress = Column(_JSON_LIST, nullable=False, default=list)
    data_egress = Column(_JSON_LIST, nullable=False, default=list)

    healthcheck_url = Column(String(500), nullable=True)
    audit_callback_url = Column(String(500), nullable=True)
    trace_callback_url = Column(String(500), nullable=True)

    classification_ceiling = Column(String(20), nullable=True)
    required_roles = Column(_JSON_LIST, nullable=False, default=list)
    is_public = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # v0.2 source-of-truth trio (doc §3 + §15.1). "env_seeded" rows are the
    # only ones the seed may upsert; "db" rows are the UI's single source of
    # truth and must survive restarts untouched.
    config_source = Column(String(20), nullable=False, server_default="db")
    env_seed_key = Column(String(150), nullable=True)
    db_editable_fields = Column(_JSON_LIST, nullable=False, default=list)
    last_seeded_at = Column(DateTime, nullable=True)

    # Carried over from platform_links (compat / ordering). See module docstring.
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    @property
    def url(self) -> str:
        """Compat alias for the legacy ``platform_links.url`` column so the
        ``PlatformLinkResponse`` façade serialises unchanged."""
        return self.entry_url


class ServiceProjectBinding(Base):
    """Binds a RegisteredService to a project (doc §13 project-entry).

    ``project_id`` is a free-form string because projects are not yet a first
    class table in this slice; the binding still powers the access-algorithm
    step 7 (project membership) and lets a service declare itself the primary
    entry for a project.
    """

    __tablename__ = "service_project_bindings"
    __table_args__ = (
        UniqueConstraint(
            "service_id", "project_id", name="uq_service_project_binding"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(
        Integer,
        ForeignKey("registered_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = Column(String(100), nullable=False)
    is_primary_entry = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    service = relationship("RegisteredService", foreign_keys=[service_id])
