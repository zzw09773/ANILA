from datetime import datetime, timezone
from sqlalchemy import Column, Integer, DateTime, ForeignKey, Index
from app.database import Base


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=False)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    request_timestamp = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    request_duration_ms = Column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_usage_user_time", "user_id", "request_timestamp"),
        Index("idx_usage_department_time", "department_id", "request_timestamp"),
        Index("idx_usage_model_time", "model_id", "request_timestamp"),
        Index("idx_usage_timestamp", "request_timestamp"),
        Index("idx_usage_apikey_time", "api_key_id", "request_timestamp"),
    )
