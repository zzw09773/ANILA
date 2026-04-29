import secrets
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(255), nullable=False, default="新對話")
    # Free-form origin tag — which frontend created this conversation.
    # Today's values: 'anila-ui' / 'anilalm' / NULL (legacy = ANILA UI).
    # See migration 0023 for the rationale.
    origin = Column(String(32), nullable=True)
    # Knowledge-base scope for ANILALM. NULL means "not collection-scoped"
    # (anila-ui rows and pre-0024 legacy). When origin='anilalm' the API
    # layer requires this to be set so the sidebar can filter by the
    # currently-open knowledge base. ON DELETE SET NULL so deleting a
    # collection doesn't drag chat history into the void. See 0024.
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="SET NULL"),
        nullable=True,
    )
    classified = Column(Boolean, nullable=False, default=False, server_default="false")
    classified_at = Column(DateTime, nullable=True)
    classified_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner = relationship("User", foreign_keys=[user_id])
    classifier = relationship("User", foreign_keys=[classified_by])
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")
    shares = relationship("ConversationShare", back_populates="conversation", cascade="all, delete-orphan")


class ConversationShare(Base):
    __tablename__ = "conversation_shares"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(64), nullable=False, unique=True, index=True, default=lambda: secrets.token_urlsafe(32))
    mode = Column(String(20), nullable=False, default="read_only")  # read_only / fork
    allow_fork = Column(Boolean, nullable=False, default=False)
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    view_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    conversation = relationship("Conversation", back_populates="shares")
    creator = relationship("User", foreign_keys=[created_by])
