"""Shared validation primitives for ANILA wire contracts.

This module is intentionally private: consumers should depend on the
versioned envelopes exported from :mod:`anila_contracts`, not on helper
types that may evolve without becoming another wire surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints


class ContractModel(BaseModel):
    """Fail-closed defaults shared by every v1 contract model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
LongIdentifier = Annotated[str, Field(min_length=1, max_length=255)]
NonEmptyString = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
Sha256Hex = Annotated[
    str,
    StringConstraints(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]

_Hashable = TypeVar("_Hashable", str, int)


def ensure_unique(values: Sequence[_Hashable], *, field_name: str) -> None:
    """Reject ambiguous repeated identifiers instead of silently deduplicating."""

    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} 不允許重複值")
