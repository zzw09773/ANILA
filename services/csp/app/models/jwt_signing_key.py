# -*- coding: utf-8 -*-
"""CSP 自己保管的 JWT 簽章金鑰。

私鑰以既有 credential 加密（由 ``SECRET_KEY`` 衍生）寫進資料庫。
狀態只允許 ``next`` → ``active`` → ``retiring`` → ``retired``。
JWKS 公布前三態；簽名只用 ``active``。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JwtSigningKey(Base):
    __tablename__ = "jwt_signing_keys"
    __table_args__ = (
        CheckConstraint(
            "state IN ('next', 'active', 'retiring', 'retired')",
            name="ck_jwt_signing_keys_state",
        ),
        # 同時只能有一把在簽、一把在候用。retired／retiring 可以有多列。
        Index(
            "uq_jwt_signing_keys_one_active",
            "state",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index(
            "uq_jwt_signing_keys_one_next",
            "state",
            unique=True,
            postgresql_where=text("state = 'next'"),
            sqlite_where=text("state = 'next'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    kid = Column(String(64), nullable=False, unique=True)
    state = Column(String(16), nullable=False)
    private_ciphertext = Column(LargeBinary, nullable=False)
    private_nonce = Column(LargeBinary, nullable=False)
    private_tag = Column(LargeBinary, nullable=False)
    # 公鑰本來就要公布，明文存放，驗證時不必解密私鑰。
    public_pem = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    retiring_at = Column(DateTime(timezone=True), nullable=True)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    # 只有從 PEM 匯入的那一把接受沒有 iat 的舊權杖。後來產生的鑰匙一律要有 iat。
    accept_missing_iat = Column(Boolean, nullable=False, default=False)
