from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class ExternalIdentity(Base):
    __tablename__ = "external_identities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider_id = Column(
        Integer,
        ForeignKey("auth_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_subject = Column(String(255), nullable=False)
    external_username = Column(String(255), nullable=True)
    external_email = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("provider_id", "external_subject", name="uq_external_identity_subject"),
    )

    user = relationship("User", lazy="joined")
    provider = relationship("AuthProvider", lazy="joined")
