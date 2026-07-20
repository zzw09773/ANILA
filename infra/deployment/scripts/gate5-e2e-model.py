#!/usr/bin/env python3
"""Tiny disposable OpenAI-compatible model used by the Gate 5 E2E harness.

The harness must exercise the official Agent through the CSP model gateway,
but it must not depend on a multi-gigabyte model stack.  This deterministic
stdlib server emits one read-only tool call, then a final answer after the
tool result.  The Agent's E2E-only ``ANILA_E2E_REQUIRE_TOOL_APPROVAL`` switch
turns that read-only call into a native SDK approval interruption.

It is mounted only by ``compose.dev.yaml`` under the ``gate5-silver`` profile;
it is not a production model and never appears in the formal image inventory.
The server deliberately logs no request body or authorization material.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MODEL_NAME = os.environ.get("GATE5_E2E_MODEL_NAME", "gate5-e2e-model")


class _Handler(BaseHTTPRequestHandler):
    server_version = "anila-gate5-e2e-model/1"

    def log_message(self, _format: str, *_args: object) -> None:
        # Request bodies and headers may contain secrets; keep this sidecar
        # silent so a failed harness cannot accidentally echo credentials.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path in {"/health", "/ready", "/v1/models"}:
            self._json(HTTPStatus.OK, {"status": "ok", "model": MODEL_NAME})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path not in {"/v1/chat/completions", "/chat/completions"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return

        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "messages required"})
            return
        has_tool_result = any(
            isinstance(item, dict) and item.get("role") == "tool" for item in messages
        )
        if has_tool_result:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "Gate 5 Silver E2E completed.",
            }
            finish_reason = "stop"
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_gate5_e2e",
                        "type": "function",
                        "function": {
                            "name": "read_document",
                            "arguments": json.dumps(
                                {"doc_id": "gate5-e2e"}, separators=(",", ":")
                            ),
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        self._json(
            HTTPStatus.OK,
            {
                "id": "gate5-e2e-completion",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", MODEL_NAME),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )


def main() -> None:
    port = int(os.environ.get("GATE5_E2E_MODEL_PORT", "8105"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
