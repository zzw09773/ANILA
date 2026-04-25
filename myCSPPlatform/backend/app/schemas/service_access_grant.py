"""Pydantic schemas for ServiceAccessGrant CRUD.

Mirrors the XOR rule on ServiceAccessGrant (exactly one of user_id /
department_id set) at the API boundary so admin clients get a 422 with a
useful error message instead of a 500 from the DB CHECK constraint.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator


class ServiceAccessGrantCreate(BaseModel):
    """Admin payload for creating a grant. Either user_id or department_id
    must be set, never both, never neither."""

    user_id: int | None = None
    department_id: int | None = None
    platform_link_id: int

    @model_validator(mode="after")
    def _check_xor(self) -> "ServiceAccessGrantCreate":
        has_user = self.user_id is not None
        has_dept = self.department_id is not None
        if has_user == has_dept:
            raise ValueError(
                "Provide exactly one of user_id or department_id "
                "(both set or both null is invalid)."
            )
        return self


class ServiceAccessGrantResponse(BaseModel):
    id: int
    user_id: int | None
    department_id: int | None
    platform_link_id: int
    granted_by: int | None
    granted_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}
