"""OW-3 訊息級自訂動作（docs/plans/ow3-message-actions-blueprint.md §Q1）。

兩表：``message_actions``（可變列 + version/body_sha256）與
``message_action_bindings``（role／department 子樹／user 聯集可見性）。
零綁定 fail-closed；可見＝綁定到本人或本人撰寫,所有角色同規則,owner／admin 皆不繞過。``users.role`` 不動。
宣告式 prompt 模板 only——無 kind／result_mode 鑑別欄。
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


class MessageAction(Base):
    __tablename__ = "message_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    label = Column(String(120), nullable=False)
    icon = Column(String(40), nullable=False)
    body = Column(Text, nullable=False)
    body_sha256 = Column(String(64), nullable=False)
    choices = Column(JSONValue, nullable=True)
    notes = Column(Text, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_by_user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_message_actions_created_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    updated_by_user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_message_actions_updated_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    bindings = relationship(
        "MessageActionBinding",
        back_populates="action",
        cascade="all, delete-orphan",
    )
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    updated_by = relationship("User", foreign_keys=[updated_by_user_id])


class MessageActionBinding(Base):
    __tablename__ = "message_action_bindings"
    __table_args__ = (
        CheckConstraint(
            "("
            "scope_type = 'role' AND role IS NOT NULL "
            "AND department_id IS NULL AND user_id IS NULL"
            ") OR ("
            "scope_type = 'department' AND department_id IS NOT NULL "
            "AND role IS NULL AND user_id IS NULL"
            ") OR ("
            "scope_type = 'user' AND user_id IS NOT NULL "
            "AND role IS NULL AND department_id IS NULL"
            ")",
            name="ck_message_action_bindings_shape",
        ),
        Index(
            "ix_message_action_bindings_role_pair",
            "action_id",
            "role",
            unique=True,
            postgresql_where=text("scope_type = 'role'"),
            sqlite_where=text("scope_type = 'role'"),
        ),
        Index(
            "ix_message_action_bindings_department_pair",
            "action_id",
            "department_id",
            unique=True,
            postgresql_where=text("scope_type = 'department'"),
            sqlite_where=text("scope_type = 'department'"),
        ),
        Index(
            "ix_message_action_bindings_user_pair",
            "action_id",
            "user_id",
            unique=True,
            postgresql_where=text("scope_type = 'user'"),
            sqlite_where=text("scope_type = 'user'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    action_id = Column(
        Integer,
        ForeignKey(
            "message_actions.id",
            name="fk_message_action_bindings_action",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    scope_type = Column(String(20), nullable=False)  # role | department | user
    role = Column(String(40), nullable=True)
    department_id = Column(
        Integer,
        ForeignKey(
            "departments.id",
            name="fk_message_action_bindings_department",
            ondelete="CASCADE",
        ),
        nullable=True,
    )
    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_message_action_bindings_user",
            ondelete="CASCADE",
        ),
        nullable=True,
    )
    created_by = Column(
        Integer,
        ForeignKey(
            "users.id",
            name="fk_message_action_bindings_created_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    action = relationship("MessageAction", back_populates="bindings")
    department = relationship("Department", foreign_keys=[department_id])
    user = relationship("User", foreign_keys=[user_id])
    creator = relationship("User", foreign_keys=[created_by])
