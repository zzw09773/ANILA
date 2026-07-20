# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance-management API contracts."""

from __future__ import annotations

from datetime import datetime

from anila_contracts import Classification
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    model_validator,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SecurityCompartmentCreate(_StrictModel):
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class SecurityCompartmentOut(_StrictModel):
    id: int
    code: str
    name: str
    description: str | None = None
    is_active: bool
    created_by_user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ClearanceGrantCreate(_StrictModel):
    subject_user_id: PositiveInt
    max_classification_level: Classification
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    basis_ticket: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def _time_order(self) -> ClearanceGrantCreate:
        if self.expires_at <= self.valid_from:
            raise ValueError("expires_at 必須晚於 valid_from")
        return self


class ClearanceGrantOut(_StrictModel):
    id: int
    subject_user_id: int
    max_classification_level: Classification
    valid_from: datetime
    expires_at: datetime
    basis_ticket: str
    issued_by_user_id: int
    revoked_at: datetime | None = None
    revoked_by_user_id: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class GrantCompartmentOut(_StrictModel):
    clearance_grant_id: int
    compartment_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class CollectionAccessGrantCreate(_StrictModel):
    membership_granted: bool = True
    need_to_know: bool = True
    basis_ticket: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def _not_inert(self) -> CollectionAccessGrantCreate:
        if not self.membership_granted and not self.need_to_know:
            raise ValueError("collection grant 至少要包含 membership 或 need-to-know")
        return self


class CollectionAccessGrantOut(_StrictModel):
    id: int
    clearance_grant_id: int
    collection_id: int
    membership_granted: bool
    need_to_know: bool
    basis_ticket: str
    issued_by_user_id: int
    revoked_at: datetime | None = None
    revoked_by_user_id: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class RequiredCompartmentAssign(_StrictModel):
    basis_ticket: str = Field(min_length=1, max_length=255)


class RequiredCompartmentOut(_StrictModel):
    compartment_id: int
    basis_ticket: str
    assigned_by_user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="forbid")


__all__ = [
    "ClearanceGrantCreate",
    "ClearanceGrantOut",
    "CollectionAccessGrantCreate",
    "CollectionAccessGrantOut",
    "GrantCompartmentOut",
    "RequiredCompartmentAssign",
    "RequiredCompartmentOut",
    "SecurityCompartmentCreate",
    "SecurityCompartmentOut",
]
