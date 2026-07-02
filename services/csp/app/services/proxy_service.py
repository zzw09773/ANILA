"""Proxy service facade: forward requests to model backends with retry + timeout.

Doc-10 Slice 1 refactor: the implementation was split — verbatim,
behavior-preserving — into the ``app.services.proxy`` package:

- ``app.services.proxy.headers``  — downstream identity (員編) + credential
  header builders, per-agent service-token cache, gateway key injection.
- ``app.services.proxy.sse``      — SSE parse / aggregate helpers.
- ``app.services.proxy.usage``    — usage serialization + token estimation.
- ``app.services.proxy.guard``    — call-time outbound SSRF re-validation.
- ``app.services.proxy.service``  — ``proxy_request`` / ``proxy_stream``.

This module remains a thin facade so existing ``app.services.proxy_service``
import paths keep working. New code should import from ``app.services.proxy``.

NOTE for tests: monkeypatching module-level *functions* on THIS module does
not reach the implementation — patch ``app.services.proxy.service`` (etc.)
instead. Patching attributes on the shared singleton objects re-exported
here (``settings``, the ``httpx`` module) still propagates as before.
"""
import logging

import httpx  # noqa: F401  — tests patch ``proxy_service.httpx.AsyncClient`` (shared module object)

from app.config import settings  # noqa: F401  — shared singleton; attribute patches propagate
from app.database import SessionLocal  # noqa: F401  — legacy module attribute
from app.models.model_registry import ModelRegistry  # noqa: F401  — legacy module attribute
from app.services import agent_credential_service  # noqa: F401  — legacy module attribute
from app.services.proxy.guard import _guard_outbound
from app.services.proxy.headers import (
    _EMPLOYEE_ID_RE,
    _PER_AGENT_TOKEN_TTL_SECONDS,
    _apply_gateway_auth,
    _get_cached_agent_token,
    _per_agent_token_cache,
    _per_agent_token_lock,
    _resolve_outgoing_service_token,
    _set_cached_agent_token,
    build_agent_headers,
    build_model_gateway_headers,
    downstream_identity,
    invalidate_agent_token_cache,
    resolve_model_gateway_key,
)
from app.services.proxy.service import (
    _get_timeout,
    build_default_anila_meta,
    proxy_request,
    proxy_stream,
)
from app.services.proxy.sse import (
    _aggregate_sse_to_chat_completion,
    _parse_sse_block,
)
from app.services.proxy.usage import (
    _estimate_token_count,
    _extract_response_text,
    _extract_stream_text,
    _flatten_content,
    _serialize_request_for_usage,
)
from app.services.usage_writer import enqueue_usage  # noqa: F401  — legacy module attribute

logger = logging.getLogger(__name__)

__all__ = [
    "_EMPLOYEE_ID_RE",
    "_PER_AGENT_TOKEN_TTL_SECONDS",
    "_aggregate_sse_to_chat_completion",
    "_apply_gateway_auth",
    "_estimate_token_count",
    "_extract_response_text",
    "_extract_stream_text",
    "_flatten_content",
    "_get_cached_agent_token",
    "_get_timeout",
    "_guard_outbound",
    "_parse_sse_block",
    "_per_agent_token_cache",
    "_per_agent_token_lock",
    "_resolve_outgoing_service_token",
    "_serialize_request_for_usage",
    "_set_cached_agent_token",
    "build_agent_headers",
    "build_default_anila_meta",
    "build_model_gateway_headers",
    "downstream_identity",
    "invalidate_agent_token_cache",
    "proxy_request",
    "proxy_stream",
    "resolve_model_gateway_key",
]
