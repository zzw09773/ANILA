"""模型端點位址設定授權（P4.6b）。

擁有者裁定：登錄／變更模型 ``endpoint_url`` 僅限平台擁有者，以及由
擁有者逐一指派的開發者。管理員不再能任意登錄候選位址（那會讓
``endpoint_group_key`` 成為拓樸確認神諭）。

本表是獨立 binding——``users.role`` 與認證服務不動。撤銷採 soft
revoke（``revoked_at``），與 ``UnitAdminAssignment`` /
``ServiceAccessGrant`` 同風格；active user 用 partial unique index，
撤銷後可再指派。
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


class EndpointAuthorGrant(Base):
    __tablename__ = "endpoint_author_grants"
    __table_args__ = (
        Index(
            "ix_endpoint_author_grants_active_user",
            "user_id",
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
            name="fk_endpoint_author_grants_user",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    granted_by = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_endpoint_author_grants_granted_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    granted_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    grantor = relationship("User", foreign_keys=[granted_by])

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
