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


class StrictContractModel(ContractModel):
    """Strict base for the v2 governance envelopes.

    v1 predates the routing contracts and intentionally retains Pydantic's
    normal coercion behaviour for backwards compatibility.  The v2 control
    plane is a trust boundary, so its scalar fields use ``Strict*`` types and
    reject values such as ``"false"`` or ``1.0``.  The model itself still
    accepts normal JSON representations (arrays, enum strings and ISO time
    strings) at the wire boundary.
    """

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
    # ``dict.popitem`` is overloaded in typeshed; the runtime implementation
    # intentionally has the same no-return mutation behaviour as all siblings.
    popitem = _immutable  # type: ignore[assignment]
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable  # type: ignore[assignment]


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
    # Python's list/mutable-sequence stubs express ``__iadd__`` with a
    # self-referential generic overload; a narrow assignment ignore is needed
    # while preserving the runtime TypeError implementation.
    __iadd__ = _immutable  # type: ignore[assignment]
    __imul__ = _immutable  # type: ignore[assignment]


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
