from datetime import datetime, timezone
from sqlalchemy import Boolean, JSON, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


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
    input_schema = Column(JSONValue, nullable=True)
    capabilities = Column(JSONValue, nullable=True)
    # health_status: unknown / healthy / unhealthy
    health_status = Column(String(20), nullable=False, default="unknown")
    # approval_status: pending / approved / rejected
    approval_status = Column(String(20), nullable=False, default="pending")
    # When true, runtime must treat every conversation routed to this agent as
    # classified / encrypted. Set by admin in the control panel.
    requires_encryption = Column(Boolean, nullable=False, default=False, server_default="false")
    approved_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Sprint 8 X / Phase A — bootstrap-then-provision flow.
    # Admin issues a single-use ``bsk-`` token via
    # ``POST /api/agents/{id}/issue-bootstrap``; agent then calls
    # ``POST /api/agents/{id}/bootstrap`` to exchange it for a long-lived
    # service token written to ``agent_credentials``. Atomic CAS on
    # ``bootstrap_token_consumed_at`` is what stops a leaked bsk- token
    # from being replayed.
    bootstrap_token_hash = Column(String(64), nullable=True)
    bootstrap_token_expires_at = Column(DateTime, nullable=True)
    bootstrap_token_consumed_at = Column(DateTime, nullable=True)
    bootstrap_token_issued_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    owner = relationship("User", foreign_keys=[owner_user_id], backref="owned_agents")
    approver = relationship("User", foreign_keys=[approved_by])
    base_model = relationship("ModelRegistry", foreign_keys=[base_model_id])
    allowed_users = relationship(
        "User",
        secondary="user_agent_permissions",
        backref="allowed_agents",
        lazy="select",
    )
