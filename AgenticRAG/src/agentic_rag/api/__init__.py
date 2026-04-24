"""API surface — FastAPI server and SSE event schema."""

from .events import EventType, ServerEvent
from .server import create_app

__all__ = ["EventType", "ServerEvent", "create_app"]
