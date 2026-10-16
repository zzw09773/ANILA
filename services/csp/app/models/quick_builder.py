"""助手流程、不可變版本與執行紀錄。"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def now():
    return datetime.now(timezone.utc)


class QuickAssistant(Base):
    __tablename__ = "quick_assistants"
    id = Column(Integer, primary_key=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    department_id = Column(
        Integer, ForeignKey("departments.id"), nullable=False, index=True
    )
    draft_revision = Column(Integer, nullable=False, default=0)
    pending_revision = Column(Integer, nullable=True)
    published_revision = Column(Integer, nullable=True)
    disabled = Column(Boolean, nullable=False, default=False)
    disabled_revision = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class QuickVersion(Base):
    __tablename__ = "quick_versions"
    __table_args__ = (
        UniqueConstraint("assistant_id", "revision", name="uq_quick_version_revision"),
    )
    id = Column(Integer, primary_key=True)
    assistant_id = Column(
        Integer,
        ForeignKey("quick_assistants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision = Column(Integer, nullable=False)
    payload = Column(JSONValue, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    status = Column(String(24), nullable=False, default="draft")
    review_reason = Column(Text, nullable=True)
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    agent_id = Column(
        Integer,
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )


@event.listens_for(QuickVersion, "before_update")
def _immutable_payload(mapper, connection, target):
    state = inspect(target)
    if any(
        state.attrs[field].history.has_changes()
        for field in (
            "payload",
            "payload_hash",
            "revision",
            "assistant_id",
            "created_by",
        )
    ):
        raise ValueError("流程版本不可原地修改（IMMUTABLE_VERSION）")


class QuickRun(Base):
    __tablename__ = "quick_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "test_request_id", name="uq_quick_run_user_nonce"),
        Index("ix_quick_run_test_lease", "is_test", "status", "lease_expires_at"),
        Index("ix_quick_run_test_lookup", "user_id", "is_test", "version_id", "id"),
    )
    id = Column(Integer, primary_key=True)
    version_id = Column(
        Integer, ForeignKey("quick_versions.id"), nullable=False, index=True
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(24), nullable=False)
    result = Column(JSONValue, nullable=False)
    # Nullable nonce preserves legacy tests which did not supply request_id.
    # The owner token is private execution authority, never a public request ID.
    is_test = Column(Boolean, nullable=False, default=False, server_default=false())
    test_request_id = Column(String(36), nullable=True)
    test_request_hash = Column(String(64), nullable=True)
    test_actor_department_id = Column(Integer, nullable=True)
    execution_owner_token = Column(String(36), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    lease_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class QuickTemplate(Base):
    __tablename__ = "quick_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=False)
    graph = Column(JSONValue, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=now, onupdate=now
    )


class QuickCollectionPublication(Base):
    __tablename__ = "quick_collection_publications"
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    scope = Column(String(20), nullable=False)
    target_department_ids = Column(JSONValue, nullable=False, default=list)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=False, default=now)
