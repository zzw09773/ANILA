"""API surface — FastAPI server and SSE event schema."""

from .events import EventType, ServerEvent


def create_app(*args, **kwargs):
    from .server import create_app as _create_app

    return _create_app(*args, **kwargs)

__all__ = ["EventType", "ServerEvent", "create_app"]
