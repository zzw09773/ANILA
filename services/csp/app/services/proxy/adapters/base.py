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
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict) and "error" in parsed:
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return {"error": {"message": raw_body[:500] or f"upstream status {status}"}}
