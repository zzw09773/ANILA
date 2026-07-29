from datetime import datetime, timezone
from sqlalchemy import (
    CheckConstraint, Column, Integer, String, Text, DateTime, ForeignKey, Index, JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # OW-1 / docs/plans/ow1-message-tree-blueprint.md — tree lookup by fork point.
        Index("ix_messages_conversation_parent", "conversation_id", "parent_id"),
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_messages_parent_not_self",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # OW-1 message tree (SYSTEM-MAP:46): nullable self-FK; NULL = root.
    # created_at alone is no longer a total order once siblings share a parent.
    parent_id = Column(
        Integer,
        ForeignKey(
            "messages.id",
            ondelete="CASCADE",
            name="fk_messages_parent_id",
        ),
        nullable=True,
    )
    role = Column(String(20), nullable=False)  # user / assistant / system / tool
    content = Column(Text, nullable=False, default="")
    # Audit fields populated from proxy/router metadata
    trace_id = Column(String(128), nullable=True, index=True)
    latency_ms = Column(Integer, nullable=True)
    model_name = Column(String(100), nullable=True)
    agent_name = Column(String(100), nullable=True)
    # Extra structured data (citations, tool calls, etc.)
    metadata_ = Column("metadata", JSONValue, nullable=True)
    # User feedback on assistant messages ('up' / 'down' / None)
    rating = Column(String(8), nullable=True)
    # ── 四級分類共通欄位(doc 08 §5,Slice 3a)────────────────────────────
    classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    conversation = relationship(
        "Conversation",
        back_populates="messages",
        foreign_keys=[conversation_id],
    )
    attachments = relationship("Attachment", back_populates="message", cascade="all, delete-orphan")
