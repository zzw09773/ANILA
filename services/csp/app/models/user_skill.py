"""使用者自訂的文字型 skill。

平台不執行內容。一列是一個版本；同一條 skill 的版本共用 ``lineage_id``。
已發布的那一版維持原列，修改另開草稿，審核通過才改由新版提供服務。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)

from app.database import Base


class UserSkill(Base):
    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("lineage_id", "version", name="uq_user_skills_lineage_version"),
        Index("ix_user_skills_owner", "owner_user_id"),
        Index("ix_user_skills_department", "department_id"),
        Index("ix_user_skills_status_scope", "status", "scope"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 第一版寫入後等於自己的 id。不設外鍵，避免同一列自己參考自己。
    lineage_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    name = Column(String(40), nullable=False)
    description = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    scope = Column(String(16), nullable=False)
    owner_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    auto_apply = Column(Boolean, nullable=False, default=False, server_default="false")
    status = Column(String(16), nullable=False)
    reject_reason = Column(Text, nullable=True)
    reviewed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class UserSkillNameClaim(Base):
    """一個還佔著名稱的 (層級, 名稱)。

    同一條 skill 的草稿與已發布可以同名，所以唯一性不放在版本列上。
    每個還佔著的名稱留一列；部分唯一索引按個人／單位／全院分開，
    避免 NULL 在 UNIQUE 裡互不相衝。
    """

    __tablename__ = "user_skill_name_claims"
    __table_args__ = (
        Index("ix_user_skill_name_claims_lineage", "lineage_id"),
        Index(
            "uq_skill_name_personal",
            "owner_user_id",
            "name_key",
            unique=True,
            sqlite_where=text("scope = 'personal'"),
            postgresql_where=text("scope = 'personal'"),
        ),
        Index(
            "uq_skill_name_unit",
            "department_id",
            "name_key",
            unique=True,
            sqlite_where=text("scope = 'unit'"),
            postgresql_where=text("scope = 'unit'"),
        ),
        Index(
            "uq_skill_name_campus",
            "name_key",
            unique=True,
            sqlite_where=text("scope = 'campus'"),
            postgresql_where=text("scope = 'campus'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    lineage_id = Column(Integer, nullable=False)
    scope = Column(String(16), nullable=False)
    owner_user_id = Column(Integer, nullable=True)
    department_id = Column(Integer, nullable=True)
    name_key = Column(String(40), nullable=False)
