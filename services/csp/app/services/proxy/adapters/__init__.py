from __future__ import annotations
import logging
from .base import BackendAdapter, PassthroughAdapter

logger = logging.getLogger(__name__)

_PASSTHROUGH = PassthroughAdapter()
_REGISTRY: dict[str, BackendAdapter] = {
    _PASSTHROUGH.name: _PASSTHROUGH,
}

def register(adapter: BackendAdapter) -> None:
    _REGISTRY[adapter.name] = adapter

def get_adapter(protocol: str | None) -> BackendAdapter:
    if protocol in _REGISTRY:
        return _REGISTRY[protocol]
    logger.warning(
        "未知 protocol %r（可能為 DB 殘值），fallback 至 openai_compatible passthrough",
        protocol,
    )
    return _PASSTHROUGH

__all__ = ["get_adapter", "register", "BackendAdapter", "PassthroughAdapter"]
