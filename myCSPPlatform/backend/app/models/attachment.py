import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, BigInteger
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

    message = relationship("Message", back_populates="attachments")
    uploader = relationship("User", foreign_keys=[uploaded_by])
