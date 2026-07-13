"""Shared validation primitives for ANILA wire contracts.

This module is intentionally private: consumers should depend on the
versioned envelopes exported from :mod:`anila_contracts`, not on helper
types that may evolve without becoming another wire surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints


class ContractModel(BaseModel):
    """Fail-closed defaults shared by every v1 contract model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class FrozenDict(dict[str, Any]):
    """JSON-serializable mapping that cannot be mutated after validation."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("contract mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class FrozenList(list[Any]):
    """JSON-serializable sequence that cannot be mutated after validation."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("contract sequences are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


def freeze_json(value: Any) -> Any:
    """Recursively freeze validated JSON while preserving JSON serialization."""

    if isinstance(value, dict):
        return FrozenDict({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenList(freeze_json(item) for item in value)
    return value


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
