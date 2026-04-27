"""Admin-issued opt-in grants that unlock a PlatformLink for a user or department.

Grants are XOR — exactly one of ``user_id`` / ``department_id`` is set, never
both, never neither (enforced by a CHECK constraint at the DB level). This
keeps the access-decision algorithm simple: walk the user's own grants, then
walk their department's grants. No surprise "matches both" overlap.

Revocation is soft (``revoked_at = now()``) rather than DELETE, so the audit
trail of "who granted access to what, when, and who revoked it" survives. A
partial unique index over active grants (``revoked_at IS NULL``) lets us
re-grant after a revoke without manual cleanup.

See ``migrations/versions/0012_add_service_access_control.py`` for the schema
and ``docs/multi-service-integration-plan.md`` §7.5 for the access-decision
algorithm this table powers.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
)
from sqlalchemy.orm import relationship

from app.database import Base


class ServiceAccessGrant(Base):
    __tablename__ = "service_access_grants"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL) <> (department_id IS NOT NULL)",
            name="ck_service_access_grants_user_xor_department",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    platform_link_id = Column(
        Integer,
        ForeignKey("platform_links.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SET NULL on grantor delete: keep audit row even if the admin who issued
    # the grant is later removed.
    granted_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    department = relationship("Department", foreign_keys=[department_id])
    platform_link = relationship("PlatformLink", foreign_keys=[platform_link_id])
    grantor = relationship("User", foreign_keys=[granted_by])

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
