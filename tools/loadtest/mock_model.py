#!/usr/bin/env python3
"""OpenAI-compatible mock. Streams tokens at a fixed rate. Stdlib only."""

from __future__ import annotations

import argparse
import asyncio
import json


def _frame(text: str) -> bytes:
    payload = {"choices": [{"delta": {"content": text}, "index": 0}]}
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


async def _read_http(reader: asyncio.StreamReader) -> tuple[str, str, bytes]:
    header = await reader.readuntil(b"\r\n\r\n")
    lines = header.decode("iso-8859-1", errors="replace").split("\r\n")
    request = lines[0]
    method, path, _ = (request.split(" ", 2) + ["", ""])[:3]
    length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip() or "0")
    body = await reader.readexactly(length) if length else b""
    return method, path.split("?", 1)[0], body


async def _write(writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str) -> None:
    head = (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def _stream(writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode())
    await writer.drain()
    if args.ttft > 0:
        await asyncio.sleep(args.ttft)
    piece = "字" * args.chunk
    sent = 0
    interval = args.chunk / args.tok_s if args.tok_s > 0 else 0
    while sent < args.tokens:
        n = min(args.chunk, args.tokens - sent)
        writer.write(_frame(piece[:n]))
        await writer.drain()
        sent += n
        if sent < args.tokens and interval > 0:
            await asyncio.sleep(interval)
    writer.write(b"data: [DONE]\n\n")
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    try:
        method, path, body = await _read_http(reader)
    except Exception:
        writer.close()
        return
    if path == "/health":
        await _write(writer, 200, b"ok", "text/plain")
        return
    if method == "GET":
        await _write(writer, 200, b'{"data":[]}', "application/json")
        return
    stream = True
    try:
        payload = json.loads(body.decode() or "{}")
        stream = bool(payload.get("stream", True))
    except Exception:
        stream = True
    if not stream:
        text = "字" * args.tokens
        data = {"choices": [{"message": {"role": "assistant", "content": text}}]}
        await _write(writer, 200, json.dumps(data).encode(), "application/json")
        return
    try:
        await _stream(writer, args)
    except Exception:
        try:
            writer.close()
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tok-s", type=float, default=30)
    parser.add_argument("--tokens", type=int, default=400)
    parser.add_argument("--ttft", type=float, default=0.3)
    parser.add_argument("--chunk", type=int, default=10)
    args = parser.parse_args()
    if args.chunk < 1:
        raise SystemExit("--chunk must be >= 1")

    async def _serve() -> None:
        async def _client(reader, writer):
            await _handle(reader, writer, args)

        server = await asyncio.start_server(_client, args.host, args.port)
        print(
            f"mock-model port={args.port} tok_s={args.tok_s} tokens={args.tokens} "
            f"ttft={args.ttft} chunk={args.chunk}",
            flush=True,
        )
        async with server:
            await server.serve_forever()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
