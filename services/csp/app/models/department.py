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
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    users = relationship("User", back_populates="department")
