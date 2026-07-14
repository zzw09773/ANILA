"""Safe UI/stream projection contract."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from ._base import ContractModel, Identifier, Sha256Hex, ensure_unique
from .classification import ClassificationLevel

SAFE_SUMMARY_SCHEMA_VERSION = "safe-summary/v1"


class SafeSummary(ContractModel):
    """Bounded, policy-attributed text safe for downstream projection.

    ``redacted`` records that the producer ran its redaction boundary.  It is
    not a claim that the input necessarily contained a secret; StreamBridge
    remains responsible for independent size and secret-pattern validation.
    """

    schema_version: Literal["safe-summary/v1"]
    text: str = Field(min_length=1, max_length=4096)
    classification: ClassificationLevel
    redacted: Literal[True]
    policy_ids: tuple[Identifier, ...] = Field(min_length=1)
    source_content_hash: Sha256Hex | None = None

    @field_validator("policy_ids")
    @classmethod
    def _policy_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique(value, field_name="policy_ids")
        return value


__all__ = ["SAFE_SUMMARY_SCHEMA_VERSION", "SafeSummary"]
