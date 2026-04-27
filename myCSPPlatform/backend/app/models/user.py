from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class UserModelPermission(Base):
    __tablename__ = "user_model_permissions"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    model_id = Column(
        Integer, ForeignKey("model_registry.id", ondelete="CASCADE"), primary_key=True
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="user")  # 'admin' / 'user' / 'developer'
    department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = Column(Boolean, default=True)
    is_approved = Column(Boolean, nullable=False, default=True, server_default="true")
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    # Sprint 6 X / B2：當 admin 把使用者切到 SSO-only 時設為 True；
    # ``authenticate_user`` 會在密碼比對之後拒絕本地登入。預設 False，
    # 不影響既有使用者。未來全域切到 SSO 時可透過 admin 介面批次更新。
    local_password_disabled = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    # Stamped on every successful authentication (local login, OIDC).
    # Powers the "上次登入" column in the admin user panel and lets audit
    # reports flag dormant accounts without scanning AuditLog.
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    allowed_models = relationship(
        "ModelRegistry",
        secondary="user_model_permissions",
        backref="allowed_users",
        lazy="select",
    )
    department = relationship("Department", back_populates="users", lazy="joined")

    @property
    def department_name(self) -> str | None:
        return self.department.name if self.department else None
