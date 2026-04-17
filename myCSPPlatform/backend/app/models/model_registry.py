from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), unique=True, nullable=False, index=True)  # e.g. "aia/asrd"
    display_name = Column(String(200), nullable=False)
    model_type = Column(String(20), nullable=False)  # 'llm' / 'vlm' / 'embedding' / 'agent'
    endpoint_url = Column(String(500), nullable=False)
    api_version = Column(String(10), default="v1")  # 'v1' / 'v2'
    is_active = Column(Boolean, default=True)
    health_status = Column(String(20), default="offline")  # 'online' / 'connecting' / 'offline'
    health_checked_at = Column(DateTime, nullable=True)
    description = Column(Text, nullable=True)
    context_window = Column(Integer, nullable=True)

    # Agent -> base model relationship
    base_model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=True)
    base_model = relationship("ModelRegistry", remote_side=[id], backref="derived_agents")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
