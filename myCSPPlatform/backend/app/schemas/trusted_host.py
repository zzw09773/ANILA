"""Pydantic schemas for /api/trusted-hosts."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class TrustedHostCreate(BaseModel):
    # Bare hostname only — admin types "gemma4" not "http://gemma4:8000/v1".
    # Service layer normalises to lowercase before DB insert. Length cap
    # matches DB column (255).
    host: str = Field(..., min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("host")
    @classmethod
    def _strip_and_lower(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("host must not be empty")
        # 不允許 scheme / port / path 出現 — 純 hostname。catch typo 早一點。
        if any(ch in value for ch in ("://", ":", "/", "?", "#")):
            raise ValueError(
                "host must be a bare hostname (no scheme / port / path); "
                "e.g. 'gemma4' or 'inference.internal'"
            )
        # 簡單合法字元集 (RFC 1123 hostname subset + IP literal letters)
        for ch in value:
            if not (ch.isalnum() or ch in ".-_"):
                raise ValueError(
                    f"host contains invalid character {ch!r}; "
                    "use letters / digits / '.' / '-' / '_' only"
                )
        return value


class TrustedHostResponse(BaseModel):
    id: int
    host: str
    note: str | None
    created_by_user_id: int | None
    created_by_username: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
