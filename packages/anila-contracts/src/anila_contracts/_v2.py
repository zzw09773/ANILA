"""Private primitives shared by the Gate 5 v2 governance contracts.

The module deliberately contains only Pydantic and standard-library types.
It is not part of the public wire surface; callers should import the
versioned envelopes from :mod:`anila_contracts`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, TypeAlias

from pydantic import Field, JsonValue, StrictBool, StrictFloat, StrictInt, StrictStr

from ._base import FrozenDict, StrictContractModel, ensure_unique, freeze_json

# IDs and enum-like tokens are intentionally ASCII.  They are control-plane
# identifiers, not user-facing text; allowing whitespace/control characters
# here makes log/audit correlation ambiguous and can turn a token into a
# prompt/control fragment.
Token = StrictStr
TokenField = Field(
    min_length=1,
    max_length=128,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$",
)
Identifier = StrictStr
IdentifierField = Field(
    min_length=1,
    max_length=255,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$",
)
BoundedText = StrictStr
JsonObject: TypeAlias = dict[str, JsonValue]

# Explicit strict aliases make the intended boundary obvious in generated
# schemas and stop Pydantic from accepting e.g. ``1.0`` for an integer limit.
StrictBoolean = StrictBool
StrictNumber = StrictFloat
StrictInteger = StrictInt

MAX_DESCRIPTION_LENGTH = 4096
MAX_QUERY_LENGTH = 16_384
MAX_REASON_CODE_LENGTH = 128
MAX_COLLECTION_ITEMS = 256


def validate_unique_tokens(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    """Reject duplicate capabilities/scopes/reason codes rather than deduping."""

    ensure_unique(values, field_name=field_name)
    return values


def validate_json_object(value: JsonObject, *, field_name: str) -> FrozenDict:
    """Validate a JSON object and freeze it for the lifetime of the model."""

    if not isinstance(value, dict):
        raise TypeError(f"{field_name} 必須是 JSON object")
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{field_name} 的 key 不得為空白")
        if len(key) > 256:
            raise ValueError(f"{field_name} 的 key 不得超過 256 字元")
    _reject_non_finite(value, path=field_name)
    return freeze_json(value)


def _reject_non_finite(value: Any, *, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} 不允許 NaN 或 Infinity")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_non_finite(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_finite(item, path=f"{path}[{index}]")


def ensure_text(value: str, *, field_name: str, max_length: int) -> str:
    """Require non-empty, non-control text for descriptions and rewritten input."""

    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必須是字串")
    if not value.strip():
        raise ValueError(f"{field_name} 不得為空白")
    if len(value) > max_length:
        raise ValueError(f"{field_name} 不得超過 {max_length} 字元")
    if any(ord(char) < 0x20 and char not in "\t\n" for char in value):
        raise ValueError(f"{field_name} 不得包含控制字元")
    return value


__all__ = [
    "MAX_COLLECTION_ITEMS",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_QUERY_LENGTH",
    "MAX_REASON_CODE_LENGTH",
    "BoundedText",
    "Identifier",
    "IdentifierField",
    "JsonObject",
    "StrictBoolean",
    "StrictContractModel",
    "StrictInteger",
    "StrictNumber",
    "Token",
    "TokenField",
    "ensure_text",
    "validate_json_object",
    "validate_unique_tokens",
]
