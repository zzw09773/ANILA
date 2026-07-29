"""單位管理員指派綁定（P1.3）。

綁在部門樹節點；權限涵蓋該節點整棵子樹。``users.role`` 與全域角色系統
不動——本表是獨立 binding。額度分配（SYSTEM-MAP「調整自己單位內的額度
分配」）刻意遞延至 credit-ledger epic，不在本包。

撤銷採 soft revoke（``revoked_at``），與 ``ServiceAccessGrant`` 同風格；
active pair 用 partial unique index，撤銷後可再指派。每節點最多 3 名
active 管理員由 service 層在 advisory lock 下強制（非 DB constraint）。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class UnitAdminAssignment(Base):
    __tablename__ = "unit_admin_assignments"
    __table_args__ = (
        Index(
            "ix_unit_admin_assignments_active_pair",
            "user_id",
            "department_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_unit_admin_assignments_user",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    department_id = Column(
        Integer,
        ForeignKey(
            "departments.id",
            name="fk_unit_admin_assignments_department",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    granted_by = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_unit_admin_assignments_granted_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    granted_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    department = relationship("Department", foreign_keys=[department_id])
    grantor = relationship("User", foreign_keys=[granted_by])

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
