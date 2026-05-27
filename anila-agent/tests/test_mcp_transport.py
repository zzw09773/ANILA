"""P1-12 MCP transport 配置擴充單元測試。

涵蓋:
- :class:`TransportConfig` 預設值 + 驗證 + ``backoff_for``。
- stdio ``connect_timeout`` 短時間真的 timeout (raise :class:`MCPTimeoutError`)。
- stdio subprocess crash + 自動 respawn(透過 ``transport_factory`` callable 模擬)。
- SSE heartbeat: idle 一段時間後下次 POST 應帶 ``X-MCP-Ping`` header(fake clock)。
- streamable HTTP reconnect after disconnect(MockTransport 模擬 ``ConnectError``
  後成功)。
- retry exhausted 應 raise :class:`MCPRetryExhaustedError`(``__cause__`` 為最後
  一次錯)。
- :class:`MCPTimeoutError` / :class:`MCPConnectionLostError` 分類正確。
- YAML loader 載入 ``transport_config`` 區段。
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest

from anila_agent.mcp import (
    MCPConnectionLostError,
    MCPRetryExhaustedError,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
    MCPTimeoutError,
    MCPTransportError,
    TransportConfig,
    load_mcp_servers_from_yaml,
)


# ---------------------------------------------------------------------------
# TransportConfig 基本驗證
# ---------------------------------------------------------------------------


def test_transport_config_defaults_match_spec() -> None:
    """:class:`TransportConfig` 預設值對齊 P1-12 spec(antigravity-style)。"""
    cfg = TransportConfig()
    assert cfg.connect_timeout == 30.0
    assert cfg.request_timeout == 60.0
    assert cfg.idle_timeout == 300.0
    assert cfg.max_retries == 3
    assert cfg.retry_backoff_seconds == 1.0
    assert cfg.heartbeat_interval is None


def test_transport_config_rejects_invalid_values() -> None:
    """非正數 timeout / 負 retry 等應 raise ``ValueError``。"""
    with pytest.raises(ValueError):
        TransportConfig(connect_timeout=0)
    with pytest.raises(ValueError):
        TransportConfig(request_timeout=-1.0)
    with pytest.raises(ValueError):
        TransportConfig(idle_timeout=0)
    with pytest.raises(ValueError):
        TransportConfig(max_retries=-1)
    with pytest.raises(ValueError):
        TransportConfig(retry_backoff_seconds=-0.5)
    with pytest.raises(ValueError):
        TransportConfig(heartbeat_interval=0)


def test_transport_config_backoff_for_exponential() -> None:
    """``backoff_for`` 為 exponential — base * 2^(attempt-1)。"""
    cfg = TransportConfig(retry_backoff_seconds=0.5)
    assert cfg.backoff_for(0) == 0.0
    assert cfg.backoff_for(1) == 0.5
    assert cfg.backoff_for(2) == 1.0
    assert cfg.backoff_for(3) == 2.0


# ---------------------------------------------------------------------------
# 共用 fake reader / writer
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, eof: bool = False) -> None:
        self.lines: deque[bytes] = deque()
        self._eof = eof

    def feed_frame(self, frame: dict[str, Any]) -> None:
        self.lines.append((json.dumps(frame) + "\n").encode("utf-8"))

    async def readline(self) -> bytes:
        if self._eof:
            return b""
        if not self.lines:
            return b""
        return self.lines.popleft()


class _FakeWriter:
    def __init__(self, reader: _FakeReader | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._reader = reader
        self.auto_responder: dict[str, Any] | None = None

    def write(self, data: bytes) -> None:
        text = data.decode("utf-8").strip()
        if not text:
            return
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        self.sent.append(frame)
        if self.auto_responder is not None and self._reader is not None and "id" in frame:
            method = frame["method"]
            responder = self.auto_responder.get(method)
            if responder is not None:
                response = {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "result": responder(frame.get("params", {})),
                }
                self._reader.feed_frame(response)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _good_responder() -> dict[str, Any]:
    """產生一個跑得通 handshake + tools/list 的 responder map。"""
    return {
        "initialize": lambda _p: {
            "protocolVersion": "2024-11-05",
            "serverInfo": {},
            "capabilities": {},
        },
        "tools/list": lambda _p: {"tools": []},
    }


# ---------------------------------------------------------------------------
# stdio: connect_timeout 真的會 timeout
# ---------------------------------------------------------------------------


class _SlowReader:
    """async readline 永遠掛著 — 用來模擬 server 不回應。"""

    async def readline(self) -> bytes:
        await asyncio.sleep(10)
        return b""


@pytest.mark.asyncio
async def test_stdio_connect_timeout_raises_mcp_timeout() -> None:
    """stdio handshake 超過 ``request_timeout`` 應拋 :class:`MCPTimeoutError`,
    並在 retry 用盡後升為 :class:`MCPRetryExhaustedError`。
    """
    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=0.05,  # 50ms 內必須回應
        idle_timeout=1.0,
        max_retries=0,
        retry_backoff_seconds=0,
    )
    reader = _SlowReader()
    writer = _FakeWriter(reader=None)
    server = MCPServerStdio(
        name="slow",
        transport_factory=(reader, writer),
        transport_config=cfg,
    )
    with pytest.raises(MCPTimeoutError):
        await server.connect()
    assert server.is_connected is False


# ---------------------------------------------------------------------------
# stdio: subprocess crash → 自動 respawn(max_retries 次)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_crash_then_respawn_succeeds() -> None:
    """第一次 spawn 給 EOF reader,第二次給能正常 handshake 的 reader/writer。
    應該在第二次成功連上,且 ``is_connected`` 為 True。
    """
    spawn_count = {"n": 0}

    def factory() -> tuple[_FakeReader | _FakeReader, _FakeWriter]:
        spawn_count["n"] += 1
        if spawn_count["n"] == 1:
            # 第一次:reader 立即 EOF → handshake 收到 EOF → MCPConnectionLostError
            bad_reader = _FakeReader(eof=True)
            bad_writer = _FakeWriter()
            return (bad_reader, bad_writer)
        # 第二次:健全的 stub
        good_reader = _FakeReader()
        good_writer = _FakeWriter(reader=good_reader)
        good_writer.auto_responder = _good_responder()
        return (good_reader, good_writer)

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=1.0,
        max_retries=2,
        retry_backoff_seconds=0,  # 測試時不等
    )
    server = MCPServerStdio(
        name="restart",
        transport_factory=factory,
        transport_config=cfg,
    )
    await server.connect()
    assert server.is_connected is True
    assert spawn_count["n"] == 2  # 第一次 fail,第二次成功
    await server.disconnect()


@pytest.mark.asyncio
async def test_stdio_retry_exhausted_raises() -> None:
    """所有 retry 都失敗時應 raise :class:`MCPRetryExhaustedError`,並把最後一次
    錯誤掛在 ``__cause__``。
    """
    spawn_count = {"n": 0}

    def factory() -> tuple[_FakeReader, _FakeWriter]:
        spawn_count["n"] += 1
        return (_FakeReader(eof=True), _FakeWriter())

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=1.0,
        max_retries=2,
        retry_backoff_seconds=0,
    )
    server = MCPServerStdio(
        name="dead",
        transport_factory=factory,
        transport_config=cfg,
    )
    with pytest.raises(MCPRetryExhaustedError) as exc_info:
        await server.connect()
    # 第一次 + 2 retry = 3
    assert spawn_count["n"] == 3
    # __cause__ 必為 connection lost(handshake EOF)
    assert isinstance(exc_info.value.__cause__, MCPConnectionLostError)


# ---------------------------------------------------------------------------
# SSE heartbeat — fake clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_heartbeat_header_after_idle() -> None:
    """``heartbeat_interval`` 設定時,idle 超過 interval 後下次 POST 應帶
    ``X-MCP-Ping`` header。用 instance ``_now`` override 做 fake clock。
    """
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if "id" not in body:
            return httpx.Response(202, content=b"")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {},
                        "capabilities": {},
                    },
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": []},
                },
            )
        return httpx.Response(404)

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=10.0,
        max_retries=0,
        retry_backoff_seconds=0,
        heartbeat_interval=5.0,
    )
    server = MCPServerStreamableHttp(
        name="hb",
        url="https://example.com/mcp/",
        transport=httpx.MockTransport(_handler),
        transport_config=cfg,
    )

    # 假時鐘 — _now 由 fake_time 控制。先設 0,handshake 全部記錄為 t=0。
    fake_time = {"t": 0.0}
    server._now = lambda: fake_time["t"]  # type: ignore[method-assign]

    await server.connect()
    # 跳到 100 秒之後 → 下次 POST idle 已遠超 heartbeat_interval=5
    fake_time["t"] = 100.0
    await server.list_tools()

    # tools/list 是 connect 後第一次 POST,其 header 必帶 X-MCP-Ping(因 idle > 5s)。
    tools_list_reqs = [
        r for r in captured if json.loads(r.content.decode())["method"] == "tools/list"
    ]
    assert len(tools_list_reqs) == 1
    assert tools_list_reqs[0].headers.get("X-MCP-Ping") == "1"


@pytest.mark.asyncio
async def test_heartbeat_not_sent_when_recent_activity() -> None:
    """剛活動完(< heartbeat_interval)不該送 ping。"""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if "id" not in body:
            return httpx.Response(202, content=b"")
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {},
                        "capabilities": {},
                    },
                },
            )
        if method == "tools/list":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(404)

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=10.0,
        max_retries=0,
        retry_backoff_seconds=0,
        heartbeat_interval=5.0,
    )
    server = MCPServerStreamableHttp(
        name="hb-recent",
        url="https://example.com/mcp/",
        transport=httpx.MockTransport(_handler),
        transport_config=cfg,
    )

    fake_time = {"t": 0.0}
    server._now = lambda: fake_time["t"]  # type: ignore[method-assign]
    await server.connect()
    fake_time["t"] = 1.0  # 只過 1 秒,< 5
    await server.list_tools()

    tools_list_reqs = [
        r for r in captured if json.loads(r.content.decode())["method"] == "tools/list"
    ]
    assert len(tools_list_reqs) == 1
    assert "X-MCP-Ping" not in tools_list_reqs[0].headers


# ---------------------------------------------------------------------------
# Streamable HTTP reconnect after disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamable_http_reconnect_after_connection_lost() -> None:
    """第一次 connect 時 transport 拋 ``ConnectError``,第二次成功。retry 應吃下
    第一次失敗、第二次連上。
    """
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        # notification(沒 id)— 直接 ack
        if "id" not in body:
            return httpx.Response(202, content=b"")
        if method == "initialize":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("simulated network drop")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {},
                        "capabilities": {},
                    },
                },
            )
        return httpx.Response(404)

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=5.0,
        max_retries=2,
        retry_backoff_seconds=0,
    )
    server = MCPServerStreamableHttp(
        name="reconnect",
        url="https://example.com/mcp/",
        transport=httpx.MockTransport(_handler),
        transport_config=cfg,
    )
    await server.connect()
    assert server.is_connected is True
    assert attempts["n"] == 2  # 第一次 fail,第二次成功
    await server.disconnect()


@pytest.mark.asyncio
async def test_streamable_http_connect_error_classified_as_connection_lost() -> None:
    """retry 用盡時最後一次 ``ConnectError`` 必須以 :class:`MCPConnectionLostError`
    被分類,並掛在 :class:`MCPRetryExhaustedError` 的 ``__cause__``。
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("always down")

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=5.0,
        max_retries=1,
        retry_backoff_seconds=0,
    )
    server = MCPServerStreamableHttp(
        name="down",
        url="https://example.com/mcp/",
        transport=httpx.MockTransport(_handler),
        transport_config=cfg,
    )
    with pytest.raises(MCPRetryExhaustedError) as exc_info:
        await server.connect()
    assert isinstance(exc_info.value.__cause__, MCPConnectionLostError)


@pytest.mark.asyncio
async def test_streamable_http_protocol_error_not_retried() -> None:
    """non-transport error(HTTP 500)走 protocol error path,**不** 重試。"""
    attempts = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="server error")

    cfg = TransportConfig(
        connect_timeout=1.0,
        request_timeout=1.0,
        idle_timeout=5.0,
        max_retries=5,  # 即使設大,也不該被消耗
        retry_backoff_seconds=0,
    )
    server = MCPServerStreamableHttp(
        name="bad",
        url="https://example.com/mcp/",
        transport=httpx.MockTransport(_handler),
        transport_config=cfg,
    )
    with pytest.raises(MCPTransportError) as exc_info:
        await server.connect()
    # 不應該是 retry exhausted — 是直接 raise 的 protocol error
    assert not isinstance(exc_info.value, MCPRetryExhaustedError)
    # 只試了一次
    assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# YAML loader 載 transport_config
# ---------------------------------------------------------------------------


def test_yaml_loader_parses_transport_config(tmp_path: Path) -> None:
    """yaml ``transport_config`` 區段應被解析並套到 server。"""
    yaml_file = tmp_path / "tc.yaml"
    yaml_file.write_text(
        """
servers:
  - name: gh
    transport: http
    url: https://example.com/mcp/
    transport_config:
      connect_timeout: 5
      request_timeout: 7.5
      idle_timeout: 60
      max_retries: 5
      retry_backoff_seconds: 0.25
      heartbeat_interval: 10
""",
        encoding="utf-8",
    )
    servers = load_mcp_servers_from_yaml(yaml_file)
    assert len(servers) == 1
    server = servers[0]
    cfg = server.transport_config
    assert cfg.connect_timeout == 5.0
    assert cfg.request_timeout == 7.5
    assert cfg.idle_timeout == 60.0
    assert cfg.max_retries == 5
    assert cfg.retry_backoff_seconds == 0.25
    assert cfg.heartbeat_interval == 10.0


def test_yaml_loader_transport_config_absent_uses_default(tmp_path: Path) -> None:
    """沒設 ``transport_config`` 時 server 應拿到模組預設,而非 None。"""
    yaml_file = tmp_path / "default.yaml"
    yaml_file.write_text(
        """
servers:
  - name: x
    transport: stdio
    command: echo
""",
        encoding="utf-8",
    )
    servers = load_mcp_servers_from_yaml(yaml_file)
    cfg = servers[0].transport_config
    assert cfg.connect_timeout == 30.0
    assert cfg.request_timeout == 60.0
    assert cfg.max_retries == 3


def test_yaml_loader_transport_config_bad_type_raises(tmp_path: Path) -> None:
    """``transport_config`` 非 mapping 時 raise ``ValueError``。"""
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text(
        """
servers:
  - name: x
    transport: stdio
    command: echo
    transport_config: "not a dict"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_mcp_servers_from_yaml(yaml_file)


# ---------------------------------------------------------------------------
# 錯誤分類繼承
# ---------------------------------------------------------------------------


def test_error_hierarchy() -> None:
    """三個新 error 都應繼承 :class:`MCPTransportError`,讓既有 ``except`` 不破。"""
    assert issubclass(MCPTimeoutError, MCPTransportError)
    assert issubclass(MCPConnectionLostError, MCPTransportError)
    assert issubclass(MCPRetryExhaustedError, MCPTransportError)
