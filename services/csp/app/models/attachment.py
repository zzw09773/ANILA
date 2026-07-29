import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, BigInteger, Text
from sqlalchemy.orm import relationship
from app.database import Base


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reference_id = Column(
        String(36), nullable=False, unique=True, index=True,
        default=lambda: str(uuid.uuid4()),
    )
    conversation_id = Column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    message_id = Column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False, default="application/octet-stream")
    size_bytes = Column(BigInteger, nullable=False, default=0)
    # Relative path under ATTACHMENT_STORAGE_PATH; never exposed directly to clients
    storage_path = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # P1.5 — async text extraction (r1_0011).
    # extract_status: pending | ok | failed | unsupported | too_large
    # (admission vs a model budget is derived at use time, not stored).
    extracted_text = Column(Text, nullable=True)
    token_count = Column(Integer, nullable=True)
    extract_status = Column(
        String(20), nullable=False, default="pending", server_default="pending",
    )
    extract_error = Column(String(500), nullable=True)
    extracted_at = Column(DateTime, nullable=True)
    # Parser page_count when reported; not a budget column — only for prompt labels.
    # Persisted so chat-time injection can show「N 頁」without re-parsing.
    page_count = Column(Integer, nullable=True)

    message = relationship("Message", back_populates="attachments")
    uploader = relationship("User", foreign_keys=[uploaded_by])
