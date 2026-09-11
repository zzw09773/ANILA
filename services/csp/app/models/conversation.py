from datetime import datetime, timezone
from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String,
    UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON
from app.database import Base

# Postgres → JSONB; SQLite create_all (pytest) → plain JSON.
_JSON_LIST = JSON().with_variant(JSONB(), "postgresql")


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
    # OW-1 / docs/plans/ow1-message-tree-blueprint.md — single active-path pointer.
    # use_alter: conversations ↔ messages would otherwise cycle create_all (SQLite).
    active_leaf_message_id = Column(
        Integer,
        ForeignKey(
            "messages.id",
            ondelete="SET NULL",
            name="fk_conversations_active_leaf_message_id",
            use_alter=True,
        ),
        nullable=True,
    )
    classified = Column(Boolean, nullable=False, default=False, server_default="false")
    classified_at = Column(DateTime(timezone=True), nullable=True)
    classified_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # P3 / Sprint 14 — Bell-LaPadula style "no write down" inheritance.
    # When TRUE, the platform set classified=true automatically because
    # this thread pulled an encrypted memory chunk into its prompt. The
    # UI uses this flag to render a different banner from manually-set
    # classification (provenance matters; see migration 0031).
    classification_inherited = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # ── 四級分類共通欄位(doc 08 §5,Slice 3a)────────────────────────────
    # 舊 boolean classified 保留為 compatibility read model(doc 08 §15
    # Step 3;鏡射規則 classified = level >= 密,由
    # app.modules.policy.service 維護,舊 latch 不破)。
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
    router_model_id = Column(
        Integer,
        ForeignKey("model_registry.id", ondelete="RESTRICT"),
        nullable=True,
    )
    router_selection_version = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner = relationship("User", foreign_keys=[user_id])
    classifier = relationship("User", foreign_keys=[classified_by])
    # Flat accessor for all rows in the conversation; user-facing rendering
    # walks active_leaf_message_id (OW-1). order_by includes id for sibling ties.
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at, Message.id",
        foreign_keys="Message.conversation_id",
    )
    shares = relationship("ConversationShare", back_populates="conversation", cascade="all, delete-orphan")
    user_metas = relationship(
        "ConversationUserMeta",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ConversationUserMeta(Base):
    """Per-user view of a conversation: star, folder, user tags.

    Not properties of the thread — if A stars a conversation shared with B,
    B must not see A's star. The system ``classified`` tag is never stored
    here; it is derived from ``conversations.classified`` on read.
    """

    __tablename__ = "conversation_user_meta"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "conversation_id",
            name="uq_conversation_user_meta_user_conv",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    starred = Column(Boolean, nullable=False, default=False, server_default="false")
    # Folder id from the user's ui_settings.folders list; "all" = unfiled.
    folder = Column(String(64), nullable=False, default="all", server_default="all")
    # User-authored tags only. Never contains the derived "classified" tag.
    user_tags = Column(_JSON_LIST, nullable=False, default=list)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    conversation = relationship("Conversation", back_populates="user_metas")
    user = relationship("User", foreign_keys=[user_id])


class ConversationShare(Base):
    """P4.3 — named share to a person XOR a department unit.

    Anonymous token links are retired (SYSTEM-MAP §分享). A department
    share reaches that node and its descendants, resolved at *read* time
    via ``_department_scope_ids`` so later re-parenting is honoured.
    Revoke = delete the row (no more server reads; no recall / no
    read-tracking).
    """

    __tablename__ = "conversation_shares"
    __table_args__ = (
        CheckConstraint(
            "(target_user_id IS NOT NULL AND target_department_id IS NULL)"
            " OR (target_user_id IS NULL AND target_department_id IS NOT NULL)",
            name="ck_conversation_shares_one_target",
        ),
        Index(
            "ix_conversation_shares_active_user",
            "conversation_id",
            "target_user_id",
            unique=True,
            postgresql_where=text("target_user_id IS NOT NULL"),
            sqlite_where=text("target_user_id IS NOT NULL"),
        ),
        Index(
            "ix_conversation_shares_active_dept",
            "conversation_id",
            "target_department_id",
            unique=True,
            postgresql_where=text("target_department_id IS NOT NULL"),
            sqlite_where=text("target_department_id IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    target_department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # ⚠ 死欄位,待 migration 一併 DROP。API 層(ShareCreate / ShareOut /
    # create_share)已不再收、不再寫、不再回傳這兩個值 —— 從來沒有任何
    # 授權判定讀過它們,分享一律唯讀。這裡暫留欄位只是因為 DROP COLUMN
    # 需要 migration,而本包無法在自己的樹上驗證(見報告)。兩者都有
    # Python 端 default,所以不帶值的 INSERT 照常成立。
    mode = Column(String(20), nullable=False, default="read_only")
    allow_fork = Column(Boolean, nullable=False, default=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    conversation = relationship("Conversation", back_populates="shares")
    creator = relationship("User", foreign_keys=[created_by])
    target_user = relationship("User", foreign_keys=[target_user_id])
    target_department = relationship("Department", foreign_keys=[target_department_id])
