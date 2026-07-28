# -*- coding: utf-8 -*-
"""Response-only Pydantic base — UTC offset on every datetime in JSON.

Why this exists
---------------
Many ORM columns are still ``timestamp without time zone`` and come back as
naive datetimes. Pydantic v2 serialises those to offsetless ISO-8601 strings
(``2026-07-27T11:19:42``). ECMAScript parses an offsetless date-time as
*local* time, so Asia/Taipei clients render UTC wall clocks eight hours off.

This base runs every JSON-mode datetime through ``as_utc()`` before the
default encoder. ``as_utc()`` is a no-op on already-aware values, so the base
stays correct after the later ``timestamptz`` column conversion and can remain
permanently.

Nested response models that also inherit this base apply the same rule.
Nested foreign models (e.g. ``anila_contracts.ExecutionGrant``) that already
require ``AwareDatetime`` keep emitting offsets on their own.

Implementation note
-------------------
The wrap field serializer's parameters must stay **unannotated**. Annotating
them as ``Any`` / ``SerializerFunctionWrapHandler`` makes Pydantic treat the
serializer return as opaque, and FastAPI then emits property schemas with
only ``title`` (no ``type`` / ``format``) — which silently destroys the
OpenAPI contract.

Request models stay on plain ``BaseModel`` — inbound timestamps may need
explicit-offset validation, which is a separate concern.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_serializer

from app.time_utils import as_utc


def _with_utc(value: datetime) -> datetime:
    """``as_utc`` accepts None; field values here are never None."""
    normalised = as_utc(value)
    assert normalised is not None
    return normalised


class ApiResponseModel(BaseModel):
    """Base for API *response* DTOs. Ensures JSON datetimes carry a UTC offset."""

    @field_serializer("*", mode="wrap", when_used="json")
    def _serialize_datetimes_utc(self, value, handler):
        # Intentionally unannotated — see module docstring.
        if isinstance(value, datetime):
            return handler(_with_utc(value))
        # list/tuple[datetime]: wrap sees the container, so normalise members
        # before the default list encoder runs.
        if isinstance(value, list) and value and all(
            isinstance(v, datetime) for v in value
        ):
            return handler([_with_utc(v) for v in value])
        if isinstance(value, tuple) and value and all(
            isinstance(v, datetime) for v in value
        ):
            return handler(tuple(_with_utc(v) for v in value))
        return handler(value)
