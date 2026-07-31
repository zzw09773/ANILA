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
    # 登入頁公開顯示。⚠ 打開等於「任何連得到登入頁的人都讀得到這則內容」——
    # 包含還沒有帳號、正在等核准的人,那正是它存在的理由(他們無處可問),
    # 但也表示這裡不該放只給院內看的東西。
    # 預設 false + server_default false:既有公告不會因為這個欄位上線而變公開。
    show_on_login = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
