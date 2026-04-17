from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base


class UserAgentPermission(Base):
    __tablename__ = "user_agent_permissions"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )


class ApiKeyAgentPermission(Base):
    __tablename__ = "api_key_agent_permissions"

    api_key_id = Column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Which base LLM this agent relies on (informational, nullable)
    base_model_id = Column(
        Integer, ForeignKey("model_registry.id", ondelete="SET NULL"), nullable=True
    )
    endpoint_url = Column(String(500), nullable=False)
    api_version = Column(String(20), nullable=False, default="v1")
    description_for_router = Column(Text, nullable=False, default="")
    input_schema = Column(JSONB, nullable=True)
    capabilities = Column(JSONB, nullable=True)
    # health_status: unknown / healthy / unhealthy
    health_status = Column(String(20), nullable=False, default="unknown")
    # approval_status: pending / approved / rejected
    approval_status = Column(String(20), nullable=False, default="pending")
    approved_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    owner = relationship("User", foreign_keys=[owner_user_id], backref="owned_agents")
    approver = relationship("User", foreign_keys=[approved_by])
    base_model = relationship("ModelRegistry", foreign_keys=[base_model_id])
    allowed_users = relationship(
        "User",
        secondary="user_agent_permissions",
        backref="allowed_agents",
        lazy="select",
    )
