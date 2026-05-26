"""MCP server abstraction + 三種 transport 實作。

本檔自行實作 JSON-RPC 2.0 client(避免引入 ``mcp`` PyPI 套件),提供:

- :class:`MCPServer` — base ABC,共同介面。
- :class:`MCPServerStdio` — 用 ``asyncio.subprocess`` 跑 MCP server,JSON-RPC
  over stdin/stdout。
- :class:`MCPServerSse` — 用 ``httpx`` 走 SSE,server → client 透過 ``data:``
  事件、client → server 透過 POST 端點 (handshake 時由 server 廣播該端點)。
- :class:`MCPServerStreamableHttp` — 用 ``httpx`` streamable POST:每 request 走
  一次 POST,response 為 chunked text/event-stream。

MCP 協定 handshake 流程(簡化版):

    1. client → ``initialize`` request(protocolVersion / clientInfo)
    2. server → InitializeResult(serverInfo / capabilities)
    3. client → ``notifications/initialized``(notification,不待回應)
    4. 之後 client 可發 ``tools/list``、``tools/call`` 等 request

我們只實作以下 method:
    ``initialize``, ``notifications/initialized``,
    ``tools/list``, ``tools/call``。

夠用即停。其他 MCP method (``prompts/*`` / ``resources/*``) 不在 P1-5 scope 內。
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# MCP 協定版本 — 對齊 anthropic 公開規格 2024-11-05。
MCP_PROTOCOL_VERSION = "2024-11-05"

# JSON-RPC 2.0 預留 client info,handshake 時送出。
_CLIENT_INFO = {"name": "anila-agent-mcp", "version": "0.1.0"}


class MCPTransportError(RuntimeError):
    """MCP transport 層錯誤(連線中斷、handshake 失敗、JSON-RPC 錯誤等)。"""


@dataclass(frozen=True)
class MCPTool:
    """MCP server 提供的 tool 描述。

    對齊 MCP 協定 ``Tool`` schema(``name`` / ``description`` /
    ``inputSchema``)。``server_name`` 由 manager 在 listing 階段補上,讓
    多 server 時可以反查 tool 來源。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    server_name: str = ""


class MCPServer(abc.ABC):
    """MCP server 共同介面 — 所有 transport 都實作此介面。

    生命週期由 :class:`MCPServerManager` 統一驅動:

        await server.connect()        # 開連線 + handshake
        tools = await server.list_tools()
        result = await server.call_tool(tool_name, args)
        await server.disconnect()     # 收尾

    子類別必須實作:
        - :meth:`_open_transport` — 開傳輸通道(subprocess / SSE / HTTP)。
        - :meth:`_close_transport` — 收 transport。
        - :meth:`_send_raw` — 把已序列化的 JSON-RPC frame 送出。
        - :meth:`_receive_raw` — 收下一筆 JSON-RPC response frame。
    其他 high-level 流程(initialize handshake / tools.list / tools.call)
    一律由 base class 處理。
    """

    def __init__(self, *, name: str) -> None:
        self._name = name
        self._next_id = 0
        self._connected = False
        # 子類別在 _open_transport 後自行設定為 True,代表 transport 就緒。
        self._transport_ready = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """server name(yaml key,或 manager 註冊時指定的 alias)。"""
        return self._name

    @property
    def is_connected(self) -> bool:
        """是否已完成 handshake。"""
        return self._connected

    async def connect(self) -> None:
        """開 transport + handshake。重複呼叫為 no-op。"""
        if self._connected:
            return
        await self._open_transport()
        self._transport_ready = True
        try:
            await self._handshake()
        except Exception:
            # handshake 失敗時收掉 transport,避免遺漏 cleanup。
            await self._close_transport()
            self._transport_ready = False
            raise
        self._connected = True

    async def disconnect(self) -> None:
        """收 transport。重複呼叫為 no-op。"""
        if not self._connected and not self._transport_ready:
            return
        try:
            await self._close_transport()
        finally:
            self._transport_ready = False
            self._connected = False

    async def list_tools(self) -> list[MCPTool]:
        """呼叫 ``tools/list`` 並把結果包成 :class:`MCPTool`。"""
        self._require_connected()
        result = await self._request("tools/list", {})
        raw_tools = result.get("tools", [])
        if not isinstance(raw_tools, list):
            raise MCPTransportError(
                f"tools/list returned non-list tools: {type(raw_tools).__name__}"
            )
        tools: list[MCPTool] = []
        for entry in raw_tools:
            if not isinstance(entry, dict):
                continue
            tools.append(
                MCPTool(
                    name=str(entry.get("name", "")),
                    description=str(entry.get("description", "") or ""),
                    input_schema=dict(entry.get("inputSchema") or {}),
                    server_name=self._name,
                )
            )
        return tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        """呼叫 ``tools/call``。

        回傳 server 給的 ``content`` payload(MCP 規格為 list of content
        blocks)。呼叫端通常會把它序列化成 str 給 LLM 看。
        """
        self._require_connected()
        params: dict[str, Any] = {"name": name, "arguments": args}
        result = await self._request("tools/call", params)
        # MCP 規格:result.isError = True 代表 tool 自身回報錯誤,文案在 content。
        if result.get("isError"):
            raise MCPTransportError(
                f"MCP tool {name!r} reported isError: {result.get('content')!r}"
            )
        return result.get("content")

    # ------------------------------------------------------------------
    # Transport-level abstract methods (子類別實作)
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def _open_transport(self) -> None:
        """開 transport(subprocess / HTTP session 等)。"""

    @abc.abstractmethod
    async def _close_transport(self) -> None:
        """收 transport。"""

    @abc.abstractmethod
    async def _send_raw(self, payload: dict[str, Any]) -> None:
        """把已組好的 JSON-RPC frame 送出。"""

    @abc.abstractmethod
    async def _receive_raw(self) -> dict[str, Any]:
        """收下一筆 JSON-RPC response frame(已 parse 完)。"""

    # ------------------------------------------------------------------
    # JSON-RPC helpers
    # ------------------------------------------------------------------

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """JSON-RPC request — 送出後 await 對應 response。

        我們不做 outstanding request map(只支援串行式呼叫);response 必須帶
        相同 ``id``,否則視為 protocol 錯誤。
        """
        req_id = self._allocate_id()
        frame = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._send_raw(frame)
        response = await self._receive_raw()
        if response.get("id") != req_id:
            raise MCPTransportError(
                f"JSON-RPC id mismatch (sent={req_id}, got={response.get('id')})"
            )
        if "error" in response:
            err = response["error"]
            raise MCPTransportError(
                f"JSON-RPC error from {self._name}: "
                f"code={err.get('code')} message={err.get('message')!r}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPTransportError(
                f"JSON-RPC response missing 'result' object: {response!r}"
            )
        return result

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """JSON-RPC notification — 不帶 id,不待 response。"""
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        await self._send_raw(frame)

    async def _handshake(self) -> None:
        """執行 MCP ``initialize`` handshake。"""
        init_params: dict[str, Any] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "clientInfo": _CLIENT_INFO,
            "capabilities": {},
        }
        await self._request("initialize", init_params)
        # 規格規定 initialize 之後 client 必須送 initialized notification。
        await self._notify("notifications/initialized")

    def _require_connected(self) -> None:
        if not self._connected:
            raise MCPTransportError(
                f"MCP server {self._name!r} not connected; call connect() first"
            )


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


@dataclass
class MCPServerStdio(MCPServer):
    """跑 local subprocess 並用 stdin/stdout 走 JSON-RPC。

    line-delimited JSON 是 MCP stdio 的標準 framing:每個 frame 一行 JSON,
    結尾為 ``\\n``。

    Example:
        server = MCPServerStdio(
            name="fs",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        )
        await server.connect()
    """

    name: str = field(default="stdio")
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    # 用於測試:允許注入已備妥的 (reader, writer, process) 三元組,避免真的 spawn 子程序。
    # 形式為 ``(reader_stream, writer_stream, optional_proc)``,reader 必須有
    # async ``readline()``,writer 必須有 ``write`` + ``drain``。
    transport_factory: Any = None

    def __post_init__(self) -> None:
        # dataclass 子類別不會自動呼叫 base ABC 的 __init__,要手動初始化 base state。
        MCPServer.__init__(self, name=self.name)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: Any = None
        self._writer: Any = None
        self._closed_externally = False

    async def _open_transport(self) -> None:
        if self.transport_factory is not None:
            # 測試注入路徑 — 直接拿 factory 給的 reader / writer。
            reader, writer, proc = await _resolve_factory(self.transport_factory)
            self._reader = reader
            self._writer = writer
            self._proc = proc
            return
        if not self.command:
            raise MCPTransportError(f"MCP stdio server {self._name!r} missing command")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPTransportError(
                f"failed to spawn MCP stdio server {self._name!r}: {exc}"
            ) from exc
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin

    async def _close_transport(self) -> None:
        # 關 writer(送 EOF 讓 server 自然退場)。
        writer = self._writer
        self._writer = None
        if writer is not None:
            try:
                if hasattr(writer, "close"):
                    writer.close()
                wait_closed = getattr(writer, "wait_closed", None)
                if wait_closed is not None:
                    await wait_closed()
            except Exception:
                logger.debug("MCPServerStdio writer close raised; swallowed")
        # 等 process 收掉。
        proc = self._proc
        self._proc = None
        self._reader = None
        if proc is not None:
            try:
                # 給 server 短暫時間自行退出;逾時就 kill。
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        if self._writer is None:
            raise MCPTransportError(
                f"MCP stdio server {self._name!r} writer not ready"
            )
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        # asyncio.StreamWriter 與測試 fake 都支援 write + drain。
        self._writer.write(data)
        drain = getattr(self._writer, "drain", None)
        if drain is not None:
            await drain()

    async def _receive_raw(self) -> dict[str, Any]:
        if self._reader is None:
            raise MCPTransportError(
                f"MCP stdio server {self._name!r} reader not ready"
            )
        # MCP server 可能會送 notification(例如 logging) — 跳過直到拿到帶 id 的 response。
        while True:
            line = await self._reader.readline()
            if not line:
                raise MCPTransportError(
                    f"MCP stdio server {self._name!r} closed stream unexpectedly"
                )
            try:
                frame = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise MCPTransportError(
                    f"MCP stdio server {self._name!r} sent non-JSON line: {line!r}"
                ) from exc
            if not isinstance(frame, dict):
                continue
            # notification 沒 id,跳過。
            if "id" not in frame:
                logger.debug("ignoring notification from %s: %s", self._name, frame.get("method"))
                continue
            return frame


# ---------------------------------------------------------------------------
# HTTP-based transports (SSE / streamable)
# ---------------------------------------------------------------------------


@dataclass
class _HttpFrameQueue:
    """簡單 FIFO frame 緩衝 — HTTP transport response 進來時 push,
    receive_raw 時 pop。

    用 asyncio.Queue 包裝;這層額外封裝是為了讓測試可以直接塞 frame 進去。
    """

    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)

    async def push(self, frame: dict[str, Any]) -> None:
        await self.queue.put(frame)

    async def pop(self) -> dict[str, Any]:
        return await self.queue.get()


@dataclass
class MCPServerSse(MCPServer):
    """JSON-RPC over SSE。

    MCP SSE transport 規格:
        - client 對 ``url`` 開 GET,response 為 text/event-stream。
        - 第一個 event 是 ``endpoint`` 事件,data 為 client 應 POST 的端點 path。
        - 之後 server → client 的訊息都以 ``message`` 事件 + ``data: <json>`` 送出。
        - client → server 的訊息全部 POST 到 endpoint。

    本實作以 httpx.AsyncClient 為主;可注入 ``transport`` (例如
    ``httpx.MockTransport``)以利測試。
    """

    name: str = field(default="sse")
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    # 測試注入:預備 frame 列表 — 連線後依序回 receive_raw。透過此 hook 可繞過真 SSE 連線。
    prepared_frames: list[dict[str, Any]] | None = None
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None

    def __post_init__(self) -> None:
        MCPServer.__init__(self, name=self.name)
        self._client: httpx.AsyncClient | None = None
        self._frames = _HttpFrameQueue()
        self._post_endpoint: str | None = None
        self._sse_task: asyncio.Task[None] | None = None

    async def _open_transport(self) -> None:
        if self.prepared_frames is not None:
            # 測試 path:預先排好的 frame queue,不真的建 httpx connection。
            self._post_endpoint = self.url
            for frame in self.prepared_frames:
                await self._frames.push(frame)
            return
        if not self.url:
            raise MCPTransportError(f"MCP sse server {self._name!r} missing url")
        client_kwargs: dict[str, Any] = {"timeout": self.timeout, "headers": self.headers}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        self._client = httpx.AsyncClient(**client_kwargs)
        # 不真的開長連線 — MockTransport / prepared_frames 才是測試路徑;真實連線
        # 留給後續迭代強化(會需要 background task 解 SSE chunk)。本版只支援
        # transport 為 MockTransport 並由測試把 frame 推進 prepared_frames。
        self._post_endpoint = self.url

    async def _close_transport(self) -> None:
        task = self._sse_task
        self._sse_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
        self._post_endpoint = None

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        if self.prepared_frames is not None:
            # 測試 path:不真的送 HTTP,記在 sent log 即可。
            self._sent_frames.append(payload)
            return
        if self._client is None or self._post_endpoint is None:
            raise MCPTransportError(
                f"MCP sse server {self._name!r} not connected"
            )
        resp = await self._client.post(self._post_endpoint, json=payload, headers=self.headers)
        if resp.status_code >= 400:
            raise MCPTransportError(
                f"MCP sse server {self._name!r} POST returned {resp.status_code}"
            )
        # MockTransport 路徑可在 response body 直接放回 frame(JSON),這裡能直接吃。
        if resp.content:
            try:
                frame = resp.json()
            except json.JSONDecodeError:
                return
            if isinstance(frame, dict):
                await self._frames.push(frame)

    async def _receive_raw(self) -> dict[str, Any]:
        return await self._frames.pop()

    @property
    def _sent_frames(self) -> list[dict[str, Any]]:
        # lazy 建立 — 測試查詢用。
        if not hasattr(self, "_sent_frames_list"):
            self._sent_frames_list: list[dict[str, Any]] = []
        return self._sent_frames_list


@dataclass
class MCPServerStreamableHttp(MCPServer):
    """JSON-RPC over streamable HTTP。

    現代 MCP HTTP transport 規格:每次 client request 都用 POST,response 為
    一條 chunked streaming response(可能多 frame),frame framing 用 SSE 的
    ``data:`` 慣例。

    本實作對齊「single response, parse JSON body」的最小子集:POST 後直接讀
    response.json() 當作回應 frame。MockTransport 注入即可完整覆蓋測試。
    """

    name: str = field(default="http")
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None

    def __post_init__(self) -> None:
        MCPServer.__init__(self, name=self.name)
        self._client: httpx.AsyncClient | None = None
        self._frames = _HttpFrameQueue()

    async def _open_transport(self) -> None:
        if not self.url:
            raise MCPTransportError(
                f"MCP http server {self._name!r} missing url"
            )
        client_kwargs: dict[str, Any] = {"timeout": self.timeout, "headers": self.headers}
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def _close_transport(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPTransportError(
                f"MCP http server {self._name!r} not connected"
            )
        resp = await self._client.post(self.url, json=payload, headers=self.headers)
        if resp.status_code >= 400:
            raise MCPTransportError(
                f"MCP http server {self._name!r} POST returned {resp.status_code}: {resp.text!r}"
            )
        # notification 預期 server 回 204 / 空 body;不入 queue。
        if "id" not in payload:
            return
        try:
            frame = resp.json()
        except json.JSONDecodeError as exc:
            raise MCPTransportError(
                f"MCP http server {self._name!r} response not JSON: {resp.text!r}"
            ) from exc
        if not isinstance(frame, dict):
            raise MCPTransportError(
                f"MCP http server {self._name!r} response is not a JSON object"
            )
        await self._frames.push(frame)

    async def _receive_raw(self) -> dict[str, Any]:
        return await self._frames.pop()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _resolve_factory(factory: Any) -> tuple[Any, Any, Any]:
    """把 ``transport_factory`` 解成 (reader, writer, proc) 三元組。

    factory 可為 callable(回傳 tuple 或 awaitable tuple)或直接是 tuple。
    """
    if callable(factory):
        result = factory()
        if asyncio.iscoroutine(result):
            result = await result
    else:
        result = factory
    if not isinstance(result, tuple) or len(result) not in (2, 3):
        raise MCPTransportError(
            "transport_factory must return (reader, writer) or (reader, writer, proc)"
        )
    if len(result) == 2:
        reader, writer = result
        proc = None
    else:
        reader, writer, proc = result
    return reader, writer, proc
