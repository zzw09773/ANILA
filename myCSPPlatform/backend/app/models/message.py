from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    conversation = relationship("Conversation", back_populates="messages")
    attachments = relationship("Attachment", back_populates="message", cascade="all, delete-orphan")
