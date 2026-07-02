"""CSP proxy package (Doc-10 Slice 1 split of the former ``proxy_service.py``).

Layout (behavior-preserving; bodies moved verbatim):

- ``headers``  — downstream identity (員編) + credential header builders,
  per-agent service-token cache, model-gateway key injection.
- ``sse``      — SSE block parsing / stream-to-completion aggregation.
- ``usage``    — usage serialization + server-side token estimation.
- ``guard``    — call-time outbound SSRF re-validation.
- ``service``  — ``proxy_request`` / ``proxy_stream`` orchestration.

``app.services.proxy_service`` remains as a thin facade over this package so
legacy import paths keep working.
"""
from app.services.proxy.guard import _guard_outbound
from app.services.proxy.headers import (
    _apply_gateway_auth,
    _get_cached_agent_token,
    _resolve_outgoing_service_token,
    _set_cached_agent_token,
    build_agent_headers,
    build_model_gateway_headers,
    downstream_identity,
    invalidate_agent_token_cache,
)
from app.services.proxy.service import (
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

__all__ = [
    "_aggregate_sse_to_chat_completion",
    "_apply_gateway_auth",
    "_estimate_token_count",
    "_extract_response_text",
    "_extract_stream_text",
    "_flatten_content",
    "_get_cached_agent_token",
    "_guard_outbound",
    "_parse_sse_block",
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
]
