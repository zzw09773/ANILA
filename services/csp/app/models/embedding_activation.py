"""Which embedding model search uses, and the rebuild that may be replacing it.

The console role (``model_registry.is_platform_embedding``) is the model
new work should move to. ``active_model_id`` is the model search uses
until a rebuild finishes. One row, id = 1.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base


class EmbeddingActivation(Base):
    __tablename__ = "embedding_activation"

    id = Column(Integer, primary_key=True)
    active_model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=True)
    previous_model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=True)
    switched_at = Column(DateTime(timezone=True), nullable=True)
    rebuild_target_model_id = Column(
        Integer, ForeignKey("model_registry.id"), nullable=True
    )
    rebuild_status = Column(String(20), nullable=True)
    rebuild_done = Column(Integer, nullable=False, default=0, server_default="0")
    rebuild_total = Column(Integer, nullable=False, default=0, server_default="0")
    rebuild_errors = Column(Integer, nullable=False, default=0, server_default="0")
    rebuild_started_at = Column(DateTime(timezone=True), nullable=True)
    rebuild_updated_at = Column(DateTime(timezone=True), nullable=True)
    rebuild_last_error = Column(Text, nullable=True)

    def touch(self, now: datetime) -> None:
        self.rebuild_updated_at = now
