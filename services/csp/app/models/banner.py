"""Admin announcement banners.

Admins post maintenance / security / policy notices that show at the top of
the chat UI for all users. Air-gapped intranet: the only broadcast channel
besides paper/word-of-mouth, so this is a real operational need.

Plain text content (level drives the colour). No code, no HTML execution.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class Banner(Base):
    __tablename__ = "banners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # info / warning / error / success — drives the banner colour.
    level = Column(String(20), nullable=False, default="info")
    content = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
