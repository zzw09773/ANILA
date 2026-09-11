"""ORM for router_model_grants — campus/department/group/user access rules."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class RouterModelGrant(Base):
    __tablename__ = "router_model_grants"
    __table_args__ = (
        CheckConstraint(
            "("
            "(scope_type = 'all' AND department_id IS NULL AND group_id IS NULL AND user_id IS NULL) OR "
            "(scope_type = 'department' AND department_id IS NOT NULL AND group_id IS NULL AND user_id IS NULL) OR "
            "(scope_type = 'group' AND group_id IS NOT NULL AND department_id IS NULL AND user_id IS NULL) OR "
            "(scope_type = 'user' AND user_id IS NOT NULL AND department_id IS NULL AND group_id IS NULL)"
            ")",
            name="ck_router_model_grants_scope_fk",
        ),
        Index(
            "uq_router_model_grants_all",
            "model_id",
            unique=True,
            postgresql_where=text("scope_type = 'all'"),
            sqlite_where=text("scope_type = 'all'"),
        ),
        Index(
            "uq_router_model_grants_department",
            "model_id",
            "department_id",
            "include_descendants",
            unique=True,
            postgresql_where=text("scope_type = 'department'"),
            sqlite_where=text("scope_type = 'department'"),
        ),
        Index(
            "uq_router_model_grants_group",
            "model_id",
            "group_id",
            unique=True,
            postgresql_where=text("scope_type = 'group'"),
            sqlite_where=text("scope_type = 'group'"),
        ),
        Index(
            "uq_router_model_grants_user",
            "model_id",
            "user_id",
            unique=True,
            postgresql_where=text("scope_type = 'user'"),
            sqlite_where=text("scope_type = 'user'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Integer, ForeignKey("model_registry.id", ondelete="CASCADE"), nullable=False, index=True)
    scope_type = Column(String(20), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    group_id = Column(Integer, ForeignKey("model_access_groups.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    include_descendants = Column(Boolean, nullable=False, default=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    model = relationship("ModelRegistry")
