"""P2-12 background task meta-tools — ``task_start`` / ``task_status`` / ``task_wait``。

對應 claude-code-src ``src/tools/TaskCreateTool/``、``TaskGetTool/``、
``TaskStopTool/``、``TaskListTool/`` 的合集 — Python 端壓縮成 3 個必要 meta-tool
(start / status / wait),加上一個 process-wide ``register_task_type`` registry
作為 LLM 觸發 callable 的白名單。

## 為什麼需要 registry

LLM **不能任意執行 Python callable**。讓 LLM 直接傳 callable reference 是
任意代碼執行(arbitrary code execution)。安全做法:

1. 開發者在伺服器啟動時先 ``register_task_type("scrape_url", scrape_url_async)``
   把可用 task type 註冊好。
2. LLM 只能透過 ``task_start(type="scrape_url", args=[...], kwargs={...})``
   觸發已註冊的 task type;傳未註冊的 type 會 fail-closed。

## 設計核心

* meta-tool 用直接構造 :class:`FunctionTool` (closure 捕捉 manager + registry),
  跟 P1-18 ``tool_search`` / ``activate_tool`` 同樣風格。
* 三個 meta-tool 預設 **非 deferred** — 一啟動就 expose 給 LLM,LLM 才看得到
  入口;`category="task"` 便於 UI 分組。
* 所有 meta-tool callback 回傳 JSON 字串(對齊 ``tool_search`` 慣例)。

## ANILA 平台典型用法

```python
from anila_agent.core.task_manager import TaskManager
from anila_agent.tools.task import (
    build_task_meta_tools,
    register_task_type,
)

async def scrape_url(url: str) -> dict:
    ...  # 跑很久

register_task_type("scrape_url", scrape_url)

manager = TaskManager(tracer=tracer)
tools = build_task_meta_tools(manager)
# tools 直接放進 Agent(tools=[...]) 即可,LLM 就能呼叫 task_start。
```
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from agents import FunctionTool

from anila_agent.core.task_manager import Task, TaskManager, TaskState
from anila_agent.tools.base import ToolMetadata, _attach_metadata

# meta-tool 固定 name(對應 claude-code-src TASK_*_TOOL_NAME 命名)。
TASK_START_TOOL_NAME = "task_start"
TASK_STATUS_TOOL_NAME = "task_status"
TASK_WAIT_TOOL_NAME = "task_wait"

# meta-tool 本身一律非 deferred,LLM 一開始就要看得到入口。
_META_TASK_TOOL_METADATA = ToolMetadata(
    is_read_only=False,
    category="task",
    cost_estimate="free",
    is_deferred=False,
)
_META_TASK_READ_METADATA = ToolMetadata(
    is_read_only=True,
    category="task",
    cost_estimate="free",
    is_deferred=False,
)

# task_wait 預設短 timeout,避免 LLM 把對話卡死;LLM 可在 args 中覆寫到上限。
DEFAULT_WAIT_TIMEOUT_SECONDS: float = 60.0
MAX_WAIT_TIMEOUT_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Task type registry — LLM 觸發 callable 的白名單
# ---------------------------------------------------------------------------


# process-wide registry — 由 ``register_task_type`` 寫入,build_task_meta_tools
# 啟動時讀取。改成 per-manager 變數會讓 LLM 看不到一致的 type list,因此暫定
# 全域。tests 之間以 ``_clear_task_type_registry`` 清空避免污染。
_TASK_TYPE_REGISTRY: dict[str, Callable[..., Awaitable[Any]]] = {}


def register_task_type(
    name: str,
    coro_factory: Callable[..., Awaitable[Any]],
    *,
    overwrite: bool = False,
) -> None:
    """註冊一個 LLM 可觸發的 task type。

    Args:
        name: task type 名稱(LLM 在 ``task_start(type=name)`` 傳入)。
            必須為非空字串,僅允許英數 / 底線 / 點 / 連字號(避免 shell 注入)。
        coro_factory: ``async def`` callable;會用 LLM 提供的 args/kwargs 呼叫。
            必須是 async function(``asyncio.iscoroutinefunction`` 為 True)。
        overwrite: 同名 type 已存在時,True 才允許覆寫;否則 raise ValueError。

    Raises:
        TypeError: ``coro_factory`` 不是 async function。
        ValueError: name 不合法 / 已存在且 overwrite=False。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("task type name must be a non-empty string")
    if not _is_safe_type_name(name):
        raise ValueError(
            f"invalid task type name: {name!r} (allow [a-zA-Z0-9_.-] only)"
        )

    import asyncio

    if not asyncio.iscoroutinefunction(coro_factory):
        raise TypeError(
            f"coro_factory for task type {name!r} must be an async function"
        )

    if name in _TASK_TYPE_REGISTRY and not overwrite:
        raise ValueError(f"task type already registered: {name!r}")

    _TASK_TYPE_REGISTRY[name] = coro_factory


def unregister_task_type(name: str) -> None:
    """移除一個已註冊的 task type(沒註冊則 no-op)。"""
    _TASK_TYPE_REGISTRY.pop(name, None)


def get_registered_task_types() -> tuple[str, ...]:
    """回傳當前已註冊的 task type 名稱(immutable tuple 避免外部 mutate)。"""
    return tuple(sorted(_TASK_TYPE_REGISTRY.keys()))


def _clear_task_type_registry() -> None:
    """測試用 — 清空 registry。"""
    _TASK_TYPE_REGISTRY.clear()


def _is_safe_type_name(name: str) -> bool:
    """驗證 type name 只含 [a-zA-Z0-9_.-]。"""
    return all(c.isalnum() or c in "_.-" for c in name)


# ---------------------------------------------------------------------------
# meta-tool 工廠
# ---------------------------------------------------------------------------


def _make_task_start_tool(manager: TaskManager) -> FunctionTool:
    """建立 ``task_start`` FunctionTool。

    LLM 呼叫格式:
        ``task_start(type="scrape_url", description="抓 x.com", args=[...],
            kwargs={...}, timeout_seconds=300.0)``

    回傳 JSON:``{"ok": true, "task_id": "...", "description": "..."}``。
    """

    async def _invoke(_ctx: Any, args_str: str) -> str:
        try:
            args: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            return _err("invalid_json")

        type_name = str(args.get("type") or "").strip()
        description = str(args.get("description") or "").strip()
        if not type_name:
            return _err("missing_type")
        if not description:
            return _err("missing_description")

        coro_factory = _TASK_TYPE_REGISTRY.get(type_name)
        if coro_factory is None:
            return _err(
                "unknown_task_type",
                extra={
                    "type": type_name,
                    "available": list(get_registered_task_types()),
                },
            )

        call_args = args.get("args") or []
        call_kwargs = args.get("kwargs") or {}
        if not isinstance(call_args, list):
            return _err("args_must_be_list")
        if not isinstance(call_kwargs, dict):
            return _err("kwargs_must_be_object")

        raw_timeout = args.get("timeout_seconds")
        timeout_seconds: float | None
        if raw_timeout is None:
            timeout_seconds = None
        else:
            try:
                timeout_seconds = float(raw_timeout)
            except (TypeError, ValueError):
                return _err("invalid_timeout")

        metadata = {"type": type_name}
        extra_meta = args.get("metadata")
        if isinstance(extra_meta, dict):
            metadata.update({str(k): v for k, v in extra_meta.items()})

        task_id = await manager.start(
            description,
            coro_factory,
            *call_args,
            timeout_seconds=timeout_seconds,
            metadata=metadata,
            **call_kwargs,
        )
        return json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "description": description,
                "type": type_name,
            },
            ensure_ascii=False,
        )

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "description": (
                    "Pre-registered task type name. Must match a key registered "
                    "via register_task_type(). LLM cannot pass arbitrary callables."
                ),
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of the task.",
            },
            "args": {
                "type": "array",
                "description": "Positional args forwarded to the task callable.",
            },
            "kwargs": {
                "type": "object",
                "description": "Keyword args forwarded to the task callable.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": (
                    "Task timeout in seconds. Defaults to manager's "
                    f"default ({int(manager._default_timeout)}s)."
                ),
            },
            "metadata": {
                "type": "object",
                "description": "Optional metadata to attach to the task.",
            },
        },
        "required": ["type", "description"],
        "additionalProperties": False,
    }
    tool = FunctionTool(
        name=TASK_START_TOOL_NAME,
        description=(
            "Start a background task identified by a pre-registered type name. "
            "Returns a task_id immediately; the task runs in the background and "
            "can be polled via task_status / task_wait."
        ),
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(tool, _META_TASK_TOOL_METADATA)


def _make_task_status_tool(manager: TaskManager) -> FunctionTool:
    """建立 ``task_status`` FunctionTool。"""

    async def _invoke(_ctx: Any, args_str: str) -> str:
        try:
            args: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            return _err("invalid_json")
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return _err("missing_task_id")
        try:
            task = await manager.status(task_id)
        except KeyError:
            return _err("task_not_found", extra={"task_id": task_id})
        return _task_payload(task)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task id returned by task_start().",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }
    tool = FunctionTool(
        name=TASK_STATUS_TOOL_NAME,
        description=(
            "Return the current state and progress of a background task. "
            "Non-blocking — returns immediately."
        ),
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(tool, _META_TASK_READ_METADATA)


def _make_task_wait_tool(manager: TaskManager) -> FunctionTool:
    """建立 ``task_wait`` FunctionTool — 短 timeout 等 task 終態。"""

    async def _invoke(_ctx: Any, args_str: str) -> str:
        try:
            args: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            return _err("invalid_json")
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return _err("missing_task_id")

        raw_wait = args.get("max_wait_seconds", DEFAULT_WAIT_TIMEOUT_SECONDS)
        try:
            max_wait = float(raw_wait)
        except (TypeError, ValueError):
            return _err("invalid_max_wait_seconds")
        # 防呆 — wait 不可比 task timeout 還長(避免無限 wait 把對話 freeze)。
        if max_wait <= 0:
            return _err("max_wait_seconds_must_be_positive")
        if max_wait > MAX_WAIT_TIMEOUT_SECONDS:
            max_wait = MAX_WAIT_TIMEOUT_SECONDS

        try:
            task = await manager.wait(task_id, timeout=max_wait)
        except KeyError:
            return _err("task_not_found", extra={"task_id": task_id})
        return _task_payload(task)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task id returned by task_start().",
            },
            "max_wait_seconds": {
                "type": "number",
                "description": (
                    "Max seconds to wait for the task to reach terminal state. "
                    f"Default {int(DEFAULT_WAIT_TIMEOUT_SECONDS)}s, "
                    f"capped at {int(MAX_WAIT_TIMEOUT_SECONDS)}s. The task is "
                    "not cancelled on timeout — only this wait returns early."
                ),
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }
    tool = FunctionTool(
        name=TASK_WAIT_TOOL_NAME,
        description=(
            "Wait up to max_wait_seconds for a background task to reach a "
            "terminal state (COMPLETED / FAILED / CANCELLED). Returns the "
            "task snapshot whether or not it completed within the timeout."
        ),
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(tool, _META_TASK_READ_METADATA)


def build_task_meta_tools(manager: TaskManager) -> list[FunctionTool]:
    """建立 3 個 task meta-tool(start / status / wait),closure 綁定 manager。

    Args:
        manager: 處理 background task 的 :class:`TaskManager`。

    Returns:
        ``[task_start, task_status, task_wait]``。
    """
    return [
        _make_task_start_tool(manager),
        _make_task_status_tool(manager),
        _make_task_wait_tool(manager),
    ]


# ---------------------------------------------------------------------------
# 小工具 — JSON 回應格式
# ---------------------------------------------------------------------------


def _err(code: str, *, extra: dict[str, Any] | None = None) -> str:
    """error 統一格式:``{"ok": false, "error": code, ...extra}``。"""
    payload: dict[str, Any] = {"ok": False, "error": code}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _task_payload(task: Task) -> str:
    """task snapshot → JSON 字串。"""
    payload = {"ok": True, **task.to_dict()}
    # state 是 enum,確保用字串
    if isinstance(payload.get("state"), TaskState):
        payload["state"] = payload["state"].value
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = [
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "MAX_WAIT_TIMEOUT_SECONDS",
    "TASK_START_TOOL_NAME",
    "TASK_STATUS_TOOL_NAME",
    "TASK_WAIT_TOOL_NAME",
    "build_task_meta_tools",
    "get_registered_task_types",
    "register_task_type",
    "unregister_task_type",
]
