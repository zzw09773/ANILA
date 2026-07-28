from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class ApiKeyModelPermission(Base):
    __tablename__ = "api_key_model_permissions"

    api_key_id = Column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    model_id = Column(
        Integer, ForeignKey("model_registry.id", ondelete="CASCADE"), primary_key=True
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    key_prefix = Column(String(8), nullable=False)
    key_suffix = Column(String(4), nullable=False)
    key_hash = Column(String(255), nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True)
    # ── W2-10 批次 1(migration r1_0040):三個欄都補 timezone=True ────────────
    # `expires_at` 是 W1-4 那個 500 的病根本體:`r1_0017` 把它建成 `sa.DateTime()`
    # (naive),而 `validate_api_key` 拿它跟 `datetime.now(timezone.utc)` 比 →
    # `TypeError: can't compare offset-naive and offset-aware datetimes`。r1_0040
    # 把 DB 側轉成 timestamptz,這裡同步宣告(只改一邊會觸發 drift 告警)。
    # ⚠ `expires_at` 的既有值來自 client request body(`schemas/api_key.py`),
    # 是本批唯一非平台自寫的欄 —— r1_0040 的例外掃描會把它逐筆列出待人工審。
    expires_at = Column(DateTime(timezone=True), nullable=True)  # None = no expiration
    # `created_at` / `last_used_at` 在乾淨 alembic 鏈上早就是 timestamptz,
    # 這裡是純粹修正 ORM 的錯誤宣告(r1_0040 不會動它們的資料)。
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", backref="api_keys")
    allowed_models = relationship(
        "ModelRegistry",
        secondary="api_key_model_permissions",
        backref="api_keys",
    )
