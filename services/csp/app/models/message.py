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
        # OW-1 / ``app/services/message_tree.py`` — tree lookup by fork point.
        Index("ix_messages_conversation_parent", "conversation_id", "parent_id"),
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_messages_parent_not_self",
        ),
        # 拇指 ↔ 分數配對:讚 6–10、爛 1–5;NULL = 只按拇指沒選數字。
        CheckConstraint(
            "("
            "rating_score IS NULL OR "
            "(rating = 'up' AND rating_score BETWEEN 6 AND 10) OR "
            "(rating = 'down' AND rating_score BETWEEN 1 AND 5)"
            ")",
            name="ck_messages_rating_score_matches_thumb",
        ),
        CheckConstraint(
            "feedback_status IS NULL OR feedback_status IN ('open', 'closed')",
            name="ck_messages_feedback_status",
        ),
        CheckConstraint(
            "feedback_resolution IS NULL OR feedback_resolution IN ('fixed', 'wont_fix', 'duplicate')",
            name="ck_messages_feedback_resolution",
        ),
        CheckConstraint(
            "feedback_status IS NULL OR feedback_status <> 'closed' OR feedback_resolution IS NOT NULL",
            name="ck_messages_feedback_closed_has_resolution",
        ),
        CheckConstraint(
            "("
            "feedback_resolution IS NULL OR feedback_resolution <> 'wont_fix' OR ("
            "feedback_resolution_note IS NOT NULL AND length(trim(feedback_resolution_note)) > 0"
            ")"
            ")",
            name="ck_messages_feedback_wont_fix_has_note",
        ),
        Index("ix_messages_feedback_status", "feedback_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # OW-1 message tree: nullable self-FK; NULL = root.
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
    # Optional fine score beside the thumb: up→6–10, down→1–5; NULL = thumb only.
    rating_score = Column(Integer, nullable=True)
    # 這次評分寫入的時間。NULL＝遷移前的舊列，未讀通知不把它算進去。
    rated_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # 差評結案。open＝未結案；closed＝已結案。NULL＝不是差評案件（含舊列與好評）。
    feedback_status = Column(String(16), nullable=True)
    # fixed＝已修好；wont_fix＝這次不修；duplicate＝與既有項目重複。
    feedback_resolution = Column(String(16), nullable=True)
    feedback_resolution_note = Column(Text, nullable=True)
    feedback_resolved_at = Column(DateTime(timezone=True), nullable=True)
    # 按下差評的那個人。結案通知只寄給這個人，不寄給其他管理員或分享對象。
    feedback_submitter_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # ── 四級分類共通欄位────────────────────────────
    classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    classification_latched_at = Column(DateTime(timezone=True), nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    conversation = relationship(
        "Conversation",
        back_populates="messages",
        foreign_keys=[conversation_id],
    )
    attachments = relationship("Attachment", back_populates="message", cascade="all, delete-orphan")
