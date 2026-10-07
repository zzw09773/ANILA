from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import JSONB
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
    # owner / admin / deputy / developer / user / system。
    # deputy 不是 admin tier，權限是另外一張允許清單。
    role = Column(String(20), nullable=False, default="user")
    department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 卡片上的姓名不另存。人資查到的姓名放這裡，沒查到就留空。
    display_name = Column(String(100), nullable=True)
    # hr：單位來自人資。manual：管理員或本人選的。空：還沒定過。
    department_source = Column(String(16), nullable=True)
    # 這個人上次從人資看到的職稱。空陣列表示查過、沒有職稱。
    hr_titles = Column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=True,
    )
    # 上次成功套用的人資查詢是什麼時候開始的。比較晚開始的那次已經寫入時，較早的結果丟掉。
    hr_lookup_started_at = Column(DateTime(timezone=True), nullable=True)
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
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    # 閒置停用寫 inactivity。手動停用與拒絕待審都清成空，刷卡才不會誤走重新核准。
    disabled_reason = Column(String(32), nullable=True)
    # Server-synced chat-UI preferences (folders / stars / tweaks). Keeps the
    # ANILA UI's per-user settings off browser localStorage so they follow the
    # user across shared PKI-card workstations.
    ui_settings = Column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
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
