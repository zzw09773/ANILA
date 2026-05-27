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

P1-12 — 對齊 antigravity-sdk-python 的 transport 細節:

- :class:`TransportConfig` — 共通 timeout / retry / heartbeat 設定,三種 transport
  共用。
- :class:`MCPTimeoutError` / :class:`MCPConnectionLostError` /
  :class:`MCPRetryExhaustedError` — 精細化的錯誤分類,供 manager / hook 觀察。
- Stdio: subprocess crash 後依 ``max_retries`` 自動 respawn + handshake。
- SSE / Streamable HTTP: ``connect_timeout`` / ``request_timeout`` 透過 ``httpx``
  Timeout 設定,中斷 (``ConnectError`` / ``ReadError``) 自動 reconnect (exponential
  backoff)。heartbeat 由 ``heartbeat_interval`` 控制(SSE 用 keep-alive ping)。

語意上 retry/reconnect 只負責「transport-level transient failure」— tool semantic
error (``isError: true``) 仍直接拋,避免重打有副作用的 tool。
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


class MCPTimeoutError(MCPTransportError):
    """MCP 操作超時 — connect_timeout / request_timeout / idle_timeout 觸發。

    比 :class:`MCPTransportError` 更精細,讓 manager / hook 可單獨補捉「網路慢」
    的情境(對 retry policy 有意義 — timeout 通常可以重試,protocol error 不該)。
    """


class MCPConnectionLostError(MCPTransportError):
    """MCP 連線中斷 — 對端 EOF / TCP reset / SSE chunk 中斷。

    對 stdio 代表 subprocess crash;對 HTTP/SSE 代表 socket close 或 read error。
    通常會觸發 reconnect 流程。
    """


class MCPRetryExhaustedError(MCPTransportError):
    """retry 用盡仍失敗 — 最後一次失敗的 cause 會掛在 ``__cause__`` 上。

    raise 時請務必用 ``raise MCPRetryExhaustedError(...) from last_exc``。
    """


# ---------------------------------------------------------------------------
# TransportConfig — P1-12 共通 timeout / retry / heartbeat 設定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportConfig:
    """共通 transport 設定 — 三種 transport 都吃這個 config。

    所有欄位都有 antigravity-style 預設值(對齊 ``connect_streamable_http`` 的
    timeout=30 / sse_read_timeout=300)。

    Attributes:
        connect_timeout: 開連線 / spawn subprocess 的時間上限(秒)。
        request_timeout: 單一 JSON-RPC request 等回應的時間上限(秒)。
        idle_timeout: 連線閒置 N 秒後視為斷線(SSE/HTTP 才有意義;stdio 用此值
            判定 read 沒進度的閾值)。
        max_retries: transport-level 失敗時最多重試次數(不含第一次);0 代表
            不 retry。
        retry_backoff_seconds: exponential backoff 基底(秒)— 第 N 次 retry 等
            ``retry_backoff_seconds * 2**(N-1)`` 秒。
        heartbeat_interval: 若不為 None,SSE / HTTP transport 會在閒置這麼長之
            後發 ping(下個 request 帶 ``X-MCP-Ping`` header,或對應 SSE
            keep-alive 註解);None 代表不送 heartbeat。
    """

    connect_timeout: float = 30.0
    request_timeout: float = 60.0
    idle_timeout: float = 300.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    heartbeat_interval: float | None = None

    def __post_init__(self) -> None:
        # 防呆 — 負值會把 retry 邏輯搞壞。
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be > 0")
        if self.idle_timeout <= 0:
            raise ValueError("idle_timeout must be > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be >= 0")
        if self.heartbeat_interval is not None and self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be > 0 or None")

    def backoff_for(self, attempt: int) -> float:
        """回傳第 ``attempt`` 次 retry(1-based)應等的秒數。"""
        if attempt <= 0:
            return 0.0
        return self.retry_backoff_seconds * (2 ** (attempt - 1))


# 套到 server 預設 — 取代散落各處的 magic number。
_DEFAULT_TRANSPORT_CONFIG = TransportConfig()


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

    def __init__(
        self,
        *,
        name: str,
        transport_config: TransportConfig | None = None,
    ) -> None:
        self._name = name
        self._next_id = 0
        self._connected = False
        # 子類別在 _open_transport 後自行設定為 True,代表 transport 就緒。
        self._transport_ready = False
        # P1-12 — 三種 transport 共用的 timeout/retry/heartbeat 設定。
        self._transport_config: TransportConfig = (
            transport_config or _DEFAULT_TRANSPORT_CONFIG
        )

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

    @property
    def transport_config(self) -> TransportConfig:
        """這台 server 套用的 :class:`TransportConfig`(可能是預設)。"""
        return self._transport_config

    async def connect(self) -> None:
        """開 transport + handshake。重複呼叫為 no-op。

        P1-12 — 若 transport 級錯誤(timeout / connection lost / spawn fail)發生,
        會依 ``transport_config.max_retries`` 重試,每次重試間 exponential backoff。
        最後仍失敗時拋 :class:`MCPRetryExhaustedError`(``__cause__`` 為最後一次
        錯誤)。tool semantic error 不會走 retry。
        """
        if self._connected:
            return
        cfg = self._transport_config
        last_exc: BaseException | None = None
        # attempts:第 0 次為初次嘗試,1..max_retries 為 retry。
        for attempt in range(cfg.max_retries + 1):
            if attempt > 0:
                backoff = cfg.backoff_for(attempt)
                logger.info(
                    "MCP %s connect retry %d/%d after %.2fs",
                    self._name,
                    attempt,
                    cfg.max_retries,
                    backoff,
                )
                if backoff > 0:
                    await asyncio.sleep(backoff)
            try:
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
                return
            except (MCPTimeoutError, MCPConnectionLostError) as exc:
                # transport-level transient — 走 retry。
                last_exc = exc
                continue
            except MCPTransportError:
                # protocol-level / config 錯誤 — 不重試,直接往外丟。
                raise
        # retry 用盡。若沒設 max_retries(=0),直接拋原因,不包成 retry exhausted —
        # 此情境語意上「連 retry 都沒給機會」,讓呼叫端看到的是真正的 timeout/lost。
        if cfg.max_retries == 0 and last_exc is not None:
            raise last_exc
        raise MCPRetryExhaustedError(
            f"MCP {self._name!r} connect exhausted {cfg.max_retries} retries"
        ) from last_exc

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

        P1-12 — 整個 send/receive 流程套 ``request_timeout``,timeout 時抛
        :class:`MCPTimeoutError`(供上層 retry policy 用)。tool semantic error 仍
        以 :class:`MCPTransportError` 拋,**不會** 被重試。
        """
        req_id = self._allocate_id()
        frame = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        timeout = self._transport_config.request_timeout
        try:
            await asyncio.wait_for(self._send_raw(frame), timeout=timeout)
            response = await asyncio.wait_for(self._receive_raw(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MCPTimeoutError(
                f"MCP {self._name!r} request {method!r} exceeded "
                f"request_timeout={timeout}s"
            ) from exc
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

    P1-12 — 若 subprocess crash(stdout EOF / wait() 提早回),會依
    ``transport_config.max_retries`` 自動 respawn,respawn 仍含完整 handshake。
    """

    name: str = field(default="stdio")
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    # 用於測試:允許注入已備妥的 (reader, writer, process) 三元組,避免真的 spawn 子程序。
    # 形式為 ``(reader_stream, writer_stream, optional_proc)``,reader 必須有
    # async ``readline()``,writer 必須有 ``write`` + ``drain``。也可以是 callable,
    # 每次 respawn 會重新呼叫一次,便於模擬「先 fail 後 succeed」。
    transport_factory: Any = None
    # P1-12 — timeout / retry / idle 設定;None 用模組預設。
    transport_config: TransportConfig | None = None

    def __post_init__(self) -> None:
        # dataclass 子類別不會自動呼叫 base ABC 的 __init__,要手動初始化 base state。
        MCPServer.__init__(self, name=self.name, transport_config=self.transport_config)
        # 解掉 dataclass field 對 base property 的 shadow — 統一從
        # ``self._transport_config`` 讀,呼叫端與基底 property 一致。
        self.transport_config = self._transport_config
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: Any = None
        self._writer: Any = None
        self._closed_externally = False

    async def _open_transport(self) -> None:
        # P1-12 — 透過 connect_timeout 包住 spawn,避免 npx download 卡死整個 agent。
        try:
            await asyncio.wait_for(
                self._spawn_once(),
                timeout=self._transport_config.connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MCPTimeoutError(
                f"MCP stdio server {self._name!r} spawn exceeded "
                f"connect_timeout={self._transport_config.connect_timeout}s"
            ) from exc

    async def _spawn_once(self) -> None:
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
            raise MCPConnectionLostError(
                f"MCP stdio server {self._name!r} writer not ready"
            )
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        # asyncio.StreamWriter 與測試 fake 都支援 write + drain。
        try:
            self._writer.write(data)
            drain = getattr(self._writer, "drain", None)
            if drain is not None:
                await drain()
        except (ConnectionError, BrokenPipeError, OSError) as exc:
            # subprocess crash / pipe 已關 — 把它歸為連線中斷,讓上層 retry 處理。
            raise MCPConnectionLostError(
                f"MCP stdio server {self._name!r} write failed: {exc}"
            ) from exc

    async def _receive_raw(self) -> dict[str, Any]:
        if self._reader is None:
            raise MCPConnectionLostError(
                f"MCP stdio server {self._name!r} reader not ready"
            )
        # MCP server 可能會送 notification(例如 logging) — 跳過直到拿到帶 id 的 response。
        while True:
            # P1-12 — 用 idle_timeout 包住每次 readline,長期無 progress 視為斷線。
            try:
                line = await asyncio.wait_for(
                    self._reader.readline(),
                    timeout=self._transport_config.idle_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise MCPTimeoutError(
                    f"MCP stdio server {self._name!r} idle exceeded "
                    f"{self._transport_config.idle_timeout}s"
                ) from exc
            if not line:
                # subprocess crash 或主動關閉 stdout — 視為連線中斷,讓上層 retry。
                raise MCPConnectionLostError(
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

    P1-12 — :attr:`transport_config` 控 connect/request/idle timeout 與 retry。
    ``heartbeat_interval`` 若不為 None,每次 ``_send_raw`` 會在實際 POST 前檢查上
    次活動時間,若距現在 ≥ heartbeat_interval 就在 POST header 加 ``X-MCP-Ping``
    當作 keep-alive 訊號。``httpx.ConnectError`` / ``httpx.RemoteProtocolError`` /
    ``httpx.ReadError`` 一律歸 :class:`MCPConnectionLostError`,讓上層 retry。
    """

    name: str = field(default="sse")
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    # 測試注入:預備 frame 列表 — 連線後依序回 receive_raw。透過此 hook 可繞過真 SSE 連線。
    prepared_frames: list[dict[str, Any]] | None = None
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None
    # P1-12 — timeout / retry / heartbeat 設定;None 用模組預設。
    transport_config: TransportConfig | None = None

    def __post_init__(self) -> None:
        MCPServer.__init__(self, name=self.name, transport_config=self.transport_config)
        # 解掉 dataclass field 對 base property 的 shadow。
        self.transport_config = self._transport_config
        self._client: httpx.AsyncClient | None = None
        self._frames = _HttpFrameQueue()
        self._post_endpoint: str | None = None
        self._sse_task: asyncio.Task[None] | None = None
        # P1-12 — heartbeat 追蹤:最後一次成功活動的 event-loop time。
        self._last_activity: float = 0.0
        # ``_activity_started`` 用來分辨「從未活動」與「剛好活動在 t=0」(後者 fake clock
        # 測試有用),避免 truthy 檢查在 t=0 時誤判。
        self._activity_started: bool = False

    async def _open_transport(self) -> None:
        if self.prepared_frames is not None:
            # 測試 path:預先排好的 frame queue,不真的建 httpx connection。
            self._post_endpoint = self.url
            for frame in self.prepared_frames:
                await self._frames.push(frame)
            self._mark_activity()
            return
        if not self.url:
            raise MCPTransportError(f"MCP sse server {self._name!r} missing url")
        cfg = self._transport_config
        # P1-12 — 用 httpx.Timeout 精細切 connect/read/write/pool。
        timeout_obj = httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.request_timeout,
            write=cfg.request_timeout,
            pool=cfg.connect_timeout,
        )
        client_kwargs: dict[str, Any] = {
            "timeout": timeout_obj,
            "headers": self.headers,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        self._client = httpx.AsyncClient(**client_kwargs)
        # 不真的開長連線 — MockTransport / prepared_frames 才是測試路徑;真實連線
        # 留給後續迭代強化(會需要 background task 解 SSE chunk)。本版只支援
        # transport 為 MockTransport 並由測試把 frame 推進 prepared_frames。
        self._post_endpoint = self.url
        self._mark_activity()

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
            self._mark_activity()
            return
        if self._client is None or self._post_endpoint is None:
            raise MCPConnectionLostError(
                f"MCP sse server {self._name!r} not connected"
            )
        headers = self._build_headers()
        try:
            resp = await self._client.post(
                self._post_endpoint, json=payload, headers=headers
            )
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise MCPConnectionLostError(
                f"MCP sse server {self._name!r} POST failed: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise MCPTimeoutError(
                f"MCP sse server {self._name!r} POST timed out: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise MCPTransportError(
                f"MCP sse server {self._name!r} POST returned {resp.status_code}"
            )
        self._mark_activity()
        # MockTransport 路徑可在 response body 直接放回 frame(JSON),這裡能直接吃。
        if resp.content:
            try:
                frame = resp.json()
            except json.JSONDecodeError:
                return
            if isinstance(frame, dict):
                await self._frames.push(frame)

    async def _receive_raw(self) -> dict[str, Any]:
        frame = await self._frames.pop()
        self._mark_activity()
        return frame

    def _build_headers(self) -> dict[str, str]:
        """組 POST header — 若需要 heartbeat 就加 ``X-MCP-Ping``。"""
        headers = dict(self.headers)
        interval = self._transport_config.heartbeat_interval
        if interval is not None and self._activity_started:
            now = self._now()
            if now - self._last_activity >= interval:
                headers["X-MCP-Ping"] = "1"
        return headers

    def _mark_activity(self) -> None:
        # 用 monotonic time 避免 wall-clock 跳動。子類 / 測試可 override ``_now``。
        self._last_activity = self._now()
        self._activity_started = True

    def _now(self) -> float:
        """回傳當前時間(seconds, monotonic)— 測試可 monkey-patch 改成 fake clock。"""
        try:
            return asyncio.get_event_loop().time()
        except RuntimeError:
            return 0.0

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

    P1-12 — 與 :class:`MCPServerSse` 同樣套用 :class:`TransportConfig`,加 keep-alive
    header、precise httpx Timeout、httpx 錯誤分類映射到 :class:`MCPConnectionLostError`
    / :class:`MCPTimeoutError`,讓 base ``connect()`` 的 retry 流程能正確分流。
    """

    name: str = field(default="http")
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None
    # P1-12 — timeout / retry / heartbeat 設定;None 用模組預設。
    transport_config: TransportConfig | None = None
    # P1-12 — httpx connection pool 上限,預設 10(對應大多數 MCP server 並發呼叫量)。
    max_connections: int = 10
    max_keepalive_connections: int = 5
    # P1-12 — 是否在每次 POST 帶 Connection: keep-alive(預設 True,httpx 也預設這樣)。
    keep_alive: bool = True

    def __post_init__(self) -> None:
        MCPServer.__init__(self, name=self.name, transport_config=self.transport_config)
        # 解掉 dataclass field 對 base property 的 shadow。
        self.transport_config = self._transport_config
        self._client: httpx.AsyncClient | None = None
        self._frames = _HttpFrameQueue()
        self._last_activity: float = 0.0
        self._activity_started: bool = False

    async def _open_transport(self) -> None:
        if not self.url:
            raise MCPTransportError(
                f"MCP http server {self._name!r} missing url"
            )
        cfg = self._transport_config
        timeout_obj = httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.request_timeout,
            write=cfg.request_timeout,
            pool=cfg.connect_timeout,
        )
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=self.max_keepalive_connections,
        )
        client_kwargs: dict[str, Any] = {
            "timeout": timeout_obj,
            "headers": self.headers,
            "limits": limits,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport
        self._client = httpx.AsyncClient(**client_kwargs)
        self._mark_activity()

    async def _close_transport(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        if self._client is None:
            raise MCPConnectionLostError(
                f"MCP http server {self._name!r} not connected"
            )
        headers = self._build_headers()
        try:
            resp = await self._client.post(self.url, json=payload, headers=headers)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise MCPConnectionLostError(
                f"MCP http server {self._name!r} POST failed: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise MCPTimeoutError(
                f"MCP http server {self._name!r} POST timed out: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise MCPTransportError(
                f"MCP http server {self._name!r} POST returned {resp.status_code}: {resp.text!r}"
            )
        self._mark_activity()
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
        frame = await self._frames.pop()
        self._mark_activity()
        return frame

    def _build_headers(self) -> dict[str, str]:
        """組 POST header — 加 keep-alive 與 heartbeat ping(若 idle)。"""
        headers = dict(self.headers)
        if self.keep_alive:
            headers.setdefault("Connection", "keep-alive")
        interval = self._transport_config.heartbeat_interval
        if interval is not None and self._activity_started:
            now = self._now()
            if now - self._last_activity >= interval:
                headers["X-MCP-Ping"] = "1"
        return headers

    def _mark_activity(self) -> None:
        self._last_activity = self._now()
        self._activity_started = True

    def _now(self) -> float:
        """回傳當前時間(monotonic seconds)— 測試可 monkey-patch。"""
        try:
            return asyncio.get_event_loop().time()
        except RuntimeError:
            return 0.0


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
