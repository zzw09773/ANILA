#!/usr/bin/env python3
"""OpenAI-compatible embedding stub for ANILA load tests.

Deliberately slow (EMBED_DELAY_MS) so concurrent search can expose
connection-pool holding across the outbound embed call. Returns a
deterministic 4096-d vector; CSP truncates to halfvec(4000).

  EMBED_DELAY_MS=500 EMBED_DIM=4096 python3 embed_stub.py
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DELAY_MS = int(os.environ.get("EMBED_DELAY_MS", "500"))
DIM = int(os.environ.get("EMBED_DIM", "4096"))
PORT = int(os.environ.get("PORT", "8080"))


def _vector_for(text: str) -> list[float]:
    """Stable pseudo-embedding from text hash (unit-ish magnitude)."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    seed = int.from_bytes(digest[:8], "big")
    for i in range(DIM):
        seed = (1103515245 * seed + 12345 + i) & 0x7FFFFFFF
        out.append(((seed % 10000) / 5000.0) - 1.0)
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/v1/models", "/"):
            self._json(200, {"status": "ok", "delay_ms": DELAY_MS, "dim": DIM})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        if self.path.rstrip("/").endswith("/embeddings") or self.path == "/v1/embeddings":
            if DELAY_MS > 0:
                time.sleep(DELAY_MS / 1000.0)
            inp = payload.get("input", "")
            if isinstance(inp, list):
                texts = [str(x) for x in inp]
            else:
                texts = [str(inp)]
            model = payload.get("model") or "loadtest-embed"
            data = []
            total = 0
            for i, t in enumerate(texts):
                vec = _vector_for(t)
                data.append({"object": "embedding", "index": i, "embedding": vec})
                total += max(1, len(t) // 4)
            self._json(
                200,
                {
                    "object": "list",
                    "data": data,
                    "model": model,
                    "usage": {"prompt_tokens": total, "total_tokens": total},
                },
            )
            return

        if self.path.rstrip("/").endswith("/chat/completions"):
            # Minimal non-streaming stub so optional chat profiles can run.
            if DELAY_MS > 0:
                time.sleep(DELAY_MS / 1000.0)
            model = payload.get("model") or "loadtest-chat"
            self._json(
                200,
                {
                    "id": "chatcmpl-loadtest",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "loadtest-ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                },
            )
            return

        self._json(404, {"error": f"no handler for {self.path}"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"embed stub on :{PORT} delay_ms={DELAY_MS} dim={DIM}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
