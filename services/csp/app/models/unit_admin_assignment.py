"""單位管理員指派綁定（P1.3）。

綁在部門樹節點；權限涵蓋該節點整棵子樹。``users.role`` 與全域角色系統
不動——本表是獨立 binding。用量額度由管理員設定，單位管理員只能查看
自己範圍內的額度。

撤銷採 soft revoke（``revoked_at``），與 ``ServiceAccessGrant`` 同風格；
active pair 用 partial unique index，撤銷後可再指派。指派的（source=manual）每節點最多 3 名，人資依職稱帶入的不計；
active 管理員由 service 層在 advisory lock 下強制（非 DB constraint）。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
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
    granted_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    # manual：畫面上授與。hr：人資職稱對出來的，登入時會收回不再符合的列。
    source = Column(String(16), nullable=False, default="manual", server_default="manual")

    user = relationship("User", foreign_keys=[user_id])
    department = relationship("Department", foreign_keys=[department_id])
    grantor = relationship("User", foreign_keys=[granted_by])

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
