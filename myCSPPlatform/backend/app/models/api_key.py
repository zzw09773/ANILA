from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Table
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
    expires_at = Column(DateTime, nullable=True)  # None = no expiration
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", backref="api_keys")
    allowed_models = relationship(
        "ModelRegistry",
        secondary="api_key_model_permissions",
        backref="api_keys",
    )
