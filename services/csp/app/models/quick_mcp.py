"""快製平台的 MCP 服務登錄與核准工具快照。"""
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


class QuickMCPService(Base):
    __tablename__ = "quick_mcp_services"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=False, default="")
    endpoint_url = Column(String(2048), nullable=False, unique=True)
    department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    is_public = Column(Boolean, nullable=False, default=False, server_default="false")
    status = Column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    governance_revision = Column(Integer, nullable=False, default=0, server_default="0")
    registered_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class QuickMCPTool(Base):
    __tablename__ = "quick_mcp_tools"
    __table_args__ = (
        UniqueConstraint("service_id", "name", name="uq_quick_mcp_tool_service_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_id = Column(
        Integer,
        ForeignKey("quick_mcp_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=False, default="")
    input_schema = Column(JSON, nullable=False)
    annotations = Column(JSON, nullable=False, default=dict)
    schema_fingerprint = Column(String(64), nullable=False)
    approved_fingerprint = Column(String(64), nullable=True)
    is_read_only = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_approved = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
