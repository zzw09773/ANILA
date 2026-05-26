"""Hook flavor 擴充 — P0-4 from anila-agent enhancement roadmap §4.4。

`anila_agent.core.hooks` 已支援 Python in-process callback flavor，本模組額外加上：

    PythonHook  — Python callable（既有 callback 的 ABC 包裝，對齊新介面）
    CommandHook — 透過 `subprocess` 執行 shell 指令；exit code 0=allow / 1=block / 其他=warn
    HttpHook    — `httpx.post(url, json=ctx)` ；2xx=allow / 4xx=block / 其他=warn
    PromptHook  — LLM 評估（stub，先給 interface，等 LLM router 接上才填）

每個 flavor 都實作 `HookABC.run(payload) -> HookOutput`，runtime 仍走原本的 `fire(...)`
聚合流程；換句話說 hook chain 上各個 flavor 可以任意組合，順序執行、任一 block 早停。

設計參考：
- 上游 TS schema：`claude-code-src/src/schemas/hooks.ts`（command / prompt / http / agent flavor）
- antigravity decorator factory：`antigravity-sdk-python/google/antigravity/hooks/hooks.py:244-288`
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

import httpx

from anila_agent.models.schemas import HookOutput

logger = logging.getLogger(__name__)


class HookFlavor(str, Enum):
    """Hook 來源類別 — 對應上游 TS `schemas/hooks.ts` 的 type 欄位。"""

    PYTHON = "python"   # in-process callable（原本就有）
    COMMAND = "command"  # shell command via subprocess
    PROMPT = "prompt"    # LLM evaluation via side model（stub）
    HTTP = "http"        # POST to URL


def _payload_to_json(payload: Any) -> str:
    """把 hook payload 轉成 JSON 字串，給 stdin / HTTP body 用。

    Hook payload 通常是 `@dataclass(frozen=True)` 物件（PreToolUseInput 等），
    這裡用 `asdict` 轉成 dict 再 dump；遇到無法序列化的值直接用 `default=str`。
    """
    if is_dataclass(payload) and not isinstance(payload, type):
        data: Any = asdict(payload)
    elif isinstance(payload, Mapping):
        data = dict(payload)
    elif hasattr(payload, "model_dump"):  # pydantic BaseModel
        data = payload.model_dump()
    else:
        data = {"value": payload}
    return json.dumps(data, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Hook ABC — 所有 flavor 一致的 entrypoint
# ---------------------------------------------------------------------------


class HookABC(ABC):
    """Hook flavor 的抽象介面。

    任何 flavor 都需要實作 `async run(payload) -> HookOutput`。
    回傳的 `HookOutput` 會交由 `core.hooks.fire(...)` 聚合（block / abort / context merge）。
    """

    flavor: HookFlavor

    @abstractmethod
    async def run(self, payload: Any) -> HookOutput:  # pragma: no cover - abstract
        """執行 hook，回傳 `HookOutput`。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# PythonHook — wrap 既有 callable
# ---------------------------------------------------------------------------


HookCallable = Callable[[Any], "HookOutput | Awaitable[HookOutput]"]


@dataclass
class PythonHook(HookABC):
    """In-process Python callable 包裝。

    `callback` 可為 sync 或 async function，回傳值必須是 `HookOutput`，
    這對齊原本 `hooks.py` 內的 `HookCallback` 契約。
    """

    callback: HookCallable
    flavor: HookFlavor = field(default=HookFlavor.PYTHON, init=False)

    async def run(self, payload: Any) -> HookOutput:
        result = self.callback(payload)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, HookOutput):
            raise TypeError(
                f"PythonHook callback {getattr(self.callback, '__qualname__', self.callback)} "
                f"returned {type(result).__name__}, expected HookOutput"
            )
        return result


# ---------------------------------------------------------------------------
# CommandHook — subprocess
# ---------------------------------------------------------------------------


@dataclass
class CommandHook(HookABC):
    """以 shell 指令當作 hook。

    執行模型：
        - stdin：payload 的 JSON 字串
        - exit code 0：allow（HookOutput 預設值）
        - exit code 1：block（reason 取 stdout / stderr 的前 1KB）
        - 其他 exit code：log warn，但不 block 也不 abort
        - stdout 若是 valid JSON，會 merge 進 HookOutput（例如 `additional_context`、`updated_input`）

    參數：
        command：shell 字串，會用 `shell=True` 執行（呼叫者要負責避免 injection）。
        timeout_sec：執行逾時，超時視為 block。
        shell：保留欄位，僅為跟上游 schema 對齊；目前一律走 `/bin/sh`。
    """

    command: str
    timeout_sec: float = 30.0
    shell: str = "bash"
    flavor: HookFlavor = field(default=HookFlavor.COMMAND, init=False)

    async def run(self, payload: Any) -> HookOutput:
        stdin_payload = _payload_to_json(payload)
        try:
            # 用 asyncio.to_thread 把 blocking subprocess 丟到 thread pool，
            # 不會卡住 event loop。
            completed = await asyncio.to_thread(
                subprocess.run,
                self.command,
                shell=True,
                input=stdin_payload,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "CommandHook timed out after %.1fs: %s", self.timeout_sec, self.command
            )
            return HookOutput(
                decision="block",
                reason=f"CommandHook 逾時（{self.timeout_sec}s）：{self.command}",
            )

        return _parse_command_result(
            command=self.command,
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


def _parse_command_result(
    *, command: str, exit_code: int, stdout: str, stderr: str
) -> HookOutput:
    """把 subprocess 結果翻成 `HookOutput`。

    - exit 0：嘗試 parse stdout 為 JSON 並 merge 到 HookOutput；parse 失敗就回預設 allow。
    - exit 1：block，reason 取 stderr/stdout（最多 1KB）。
    - 其他：log warn，回預設 allow（避免 hook 因為環境奇怪就把整個 turn 砍掉）。
    """
    if exit_code == 0:
        merged = _merge_hook_output_from_stdout(stdout)
        return merged if merged is not None else HookOutput()

    if exit_code == 1:
        reason = (stderr or stdout or f"command 回傳 exit code 1: {command}").strip()[
            :1024
        ]
        return HookOutput(decision="block", reason=reason)

    logger.warning(
        "CommandHook 回傳預期外的 exit code %d（command=%s, stderr=%s）",
        exit_code,
        command,
        stderr.strip()[:256],
    )
    return HookOutput()


def _merge_hook_output_from_stdout(stdout: str) -> HookOutput | None:
    """若 stdout 是合法 JSON 且為 dict，嘗試當作 HookOutput 結構 parse。"""
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return HookOutput.model_validate(data)
    except Exception:  # noqa: BLE001 — pydantic ValidationError 等都不該炸 hook
        logger.warning("CommandHook stdout 不是合法 HookOutput JSON：%s", text[:256])
        return None


# ---------------------------------------------------------------------------
# HttpHook — POST 到 webhook
# ---------------------------------------------------------------------------


@dataclass
class HttpHook(HookABC):
    """以 HTTP webhook 當作 hook。

    執行模型：
        - 對 `url` 發 POST，body 是 payload 的 JSON。
        - 2xx：allow；response body 若是 JSON dict 會 merge 進 HookOutput。
        - 4xx：block，reason 取 response body（前 1KB）。
        - 其他（5xx / 連線失敗等）：log warn，回預設 allow（webhook 不該因 server 暫時掛掉就 block tool）。

    參數：
        url：webhook endpoint。
        timeout_sec：請求逾時，超時視為 block。
        headers：自訂 header；caller 自行做環境變數內插。
        client：可選注入 `httpx.AsyncClient`，方便測試 mock。
    """

    url: str
    timeout_sec: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)
    client: httpx.AsyncClient | None = None
    flavor: HookFlavor = field(default=HookFlavor.HTTP, init=False)

    async def run(self, payload: Any) -> HookOutput:
        body = _payload_to_json(payload)
        request_headers = {"content-type": "application/json", **self.headers}

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout_sec)
        try:
            response = await client.post(
                self.url, content=body, headers=request_headers, timeout=self.timeout_sec
            )
        except httpx.TimeoutException:
            logger.warning("HttpHook 逾時：%s", self.url)
            return HookOutput(
                decision="block",
                reason=f"HttpHook 逾時（{self.timeout_sec}s）：{self.url}",
            )
        except httpx.HTTPError as exc:
            logger.warning("HttpHook 連線錯誤：%s（%s）", self.url, exc)
            return HookOutput()
        finally:
            if owns_client:
                await client.aclose()

        return _parse_http_response(url=self.url, response=response)


def _parse_http_response(*, url: str, response: httpx.Response) -> HookOutput:
    """把 httpx 回應翻成 `HookOutput`。"""
    status = response.status_code
    if 200 <= status < 300:
        merged = _merge_hook_output_from_stdout(response.text)
        return merged if merged is not None else HookOutput()
    if 400 <= status < 500:
        reason = (response.text or f"HTTP {status}").strip()[:1024]
        return HookOutput(decision="block", reason=reason)
    logger.warning(
        "HttpHook 回傳預期外的 status %d（url=%s, body=%s）",
        status,
        url,
        response.text.strip()[:256],
    )
    return HookOutput()


# ---------------------------------------------------------------------------
# PromptHook — stub
# ---------------------------------------------------------------------------


@dataclass
class PromptHook(HookABC):
    """LLM 評估 hook — 目前為 stub，等 LLM router 接上才實作。

    上游 schema 行為：`prompt_template` 內含 `$ARGUMENTS` placeholder，
    runtime 會把 hook payload JSON 代入 placeholder，送給 `model` 評估，
    LLM 回 YES / NO 對應 allow / block。

    Stub 階段：呼叫 `run` 一律 raise `NotImplementedError`，避免 silent allow 造成
    安全假象；caller 在 LLM router 還沒接上前不應註冊 PromptHook。
    """

    prompt_template: str
    model: str | None = None
    timeout_sec: float = 30.0
    flavor: HookFlavor = field(default=HookFlavor.PROMPT, init=False)

    async def run(self, payload: Any) -> HookOutput:
        raise NotImplementedError(
            "PromptHook 尚未實作 — 等待 LLM router 接上後才會啟用。"
            f" prompt_template={self.prompt_template!r}, model={self.model!r}"
        )


__all__ = [
    "HookABC",
    "HookFlavor",
    "PythonHook",
    "CommandHook",
    "HttpHook",
    "PromptHook",
]
