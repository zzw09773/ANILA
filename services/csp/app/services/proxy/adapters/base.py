from __future__ import annotations
import json
from typing import Protocol, runtime_checkable
import httpx

_V2 = ("/v1", "/v2")

@runtime_checkable
class BackendAdapter(Protocol):
    name: str
    def request_timeout(self, endpoint_kind: str, default: httpx.Timeout) -> httpx.Timeout: ...
    def backend_path(self, endpoint_kind: str, model_name: str, api_version: str) -> str: ...
    def to_backend_request(self, endpoint_kind: str, body: dict) -> dict: ...
    def from_backend_response(self, endpoint_kind: str, resp: dict) -> dict: ...
    def from_backend_stream_chunk(self, endpoint_kind: str, raw_line: str) -> str | None: ...
    def from_backend_error(self, endpoint_kind: str, status: int, raw_body: str) -> dict: ...

_KIND_PATH = {"chat": "chat/completions", "embeddings": "embeddings", "models": "models"}

class PassthroughAdapter:
    """openai_compatible：全 no-op，行為 == 現況。"""
    name = "openai_compatible"

    def request_timeout(self, endpoint_kind: str, default: httpx.Timeout) -> httpx.Timeout:
        return default

    def backend_path(self, endpoint_kind: str, model_name: str, api_version: str) -> str:
        return f"/{api_version}/{_KIND_PATH[endpoint_kind]}"

    def to_backend_request(self, endpoint_kind: str, body: dict) -> dict:
        return body

    def from_backend_response(self, endpoint_kind: str, resp: dict) -> dict:
        return resp

    def from_backend_stream_chunk(self, endpoint_kind: str, raw_line: str) -> str | None:
        return raw_line

    def from_backend_error(self, endpoint_kind: str, status: int, raw_body: str) -> dict:
        """Fail-safe 4xx translation: only a well-formed OpenAI-shape body
        (JSON dict, ``error`` is a dict, ``error.message`` a non-empty str)
        surfaces its message (truncated to 300 chars) to the client. Every
        other shape — non-JSON, no ``error`` key, ``error`` not a dict,
        missing/empty/non-str ``message`` — falls back to a generic message.
        The raw upstream body is NEVER echoed back here; it may carry
        internal backend detail (stack traces, internal hostnames, API key
        hints) that must stay server-side-log-only (logged by the caller in
        ``service.py``)."""
        generic = f"模型服務拒絕請求 (HTTP {status})"
        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            return {"error": {"message": generic}}
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                msg = error.get("message")
                if isinstance(msg, str) and msg:
                    return {"error": {"message": msg[:300]}}
        return {"error": {"message": generic}}
