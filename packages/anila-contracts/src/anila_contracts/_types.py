"""Closed vocabularies shared by the Gate 2 v1 contracts."""

from __future__ import annotations

from enum import Enum


class SourceScope(str, Enum):
    NONE = "none"
    PERSONAL = "personal"
    PROJECT = "project"
    ORGANIZATION = "organization"
    REGISTERED_SERVICE = "registered_service"


class SnapshotOrigin(str, Enum):
    COLLECTION = "collection"
    DOCUMENT = "document"
    UPLOAD = "upload"
    NONE = "none"
    SERVICE = "service"


class InvocationTargetKind(str, Enum):
    MODEL = "model"
    AGENT = "agent"
    STUDIO = "studio"
    SERVICE = "service"
    RETRIEVAL = "retrieval"


__all__ = ["InvocationTargetKind", "SnapshotOrigin", "SourceScope"]
