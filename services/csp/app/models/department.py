from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import relationship
from app.database import Base


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (
        Index(
            "ix_departments_parent_id",
            "parent_id",
            postgresql_where=text("parent_id IS NOT NULL"),
            sqlite_where=text("parent_id IS NOT NULL"),
        ),
        # 名稱唯一性以「同一個母單位之下」為界，不是全院唯一：兩個所可以各有
        # 一個「企劃組」，這是院內編制的常態，全域 unique 會直接擋掉。
        Index(
            "uq_departments_parent_id_name",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL"),
            sqlite_where=text("parent_id IS NOT NULL"),
        ),
        # NULL 在 UNIQUE 索引裡兩兩不相等，所以上面那個索引擋不住兩個同名的
        # 根節點（兩個「院部」）。根層另外用一個偏索引補起來。
        Index(
            "uq_departments_root_name",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
            sqlite_where=text("parent_id IS NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, index=True)
    description = Column(String(255), nullable=True)
    # NULL parent = 根節點（院）；深度由鏈推導，不存 level/path
    parent_id = Column(
        Integer,
        ForeignKey(
            "departments.id",
            name="fk_departments_parent_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    users = relationship("User", back_populates="department")
