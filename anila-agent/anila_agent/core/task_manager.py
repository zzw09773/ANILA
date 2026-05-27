"""P2-12 Task / background task — `TaskManager` + `Task` + `TaskState`。

對應 claude-code-src ``src/Task.ts`` + ``src/tasks/`` 整套 background task lifecycle
(LocalAgentTask / LocalShellTask / DreamTask / RemoteAgentTask),以 Python /
asyncio 風格落地。

## 問題場景

agent 想啟動「跑很久的工作」(scrape 大檔、跑長 sub-agent、處理 batch)時不應該
阻塞當前對話。LLM 需要的是:

1. 呼叫 ``task_start(description, type, args)`` 啟動 task,馬上拿到 ``task_id``。
2. 之後對話可以 ``task_status(task_id)`` 查狀態 / ``task_wait(task_id)`` 短 timeout 等。
3. task 完成可選 fire trigger callback,把訊息注入下一輪 agent prompt(對應
   user case「studio job 完成自動續推」)。

## 設計核心

* **Task isolation**:每個 task 跑在獨立 ``asyncio.Task``,例外 catch 後寫入
  ``task.error`` 與 ``state=FAILED``,主 agent loop 不受影響。
* **Timeout 預設 300s**:可用 ``timeout_seconds`` 參數覆寫;timeout 後 task
  state 設為 ``FAILED`` 且 error 為 ``"timeout: ..."``。
* **Cancel**:``cancel(task_id)`` 把 underlying ``asyncio.Task`` cancel;
  task state 轉為 ``CANCELLED``,``CancelledError`` 不會 propagate 出去。
* **Tracing**:每個 task 一條 trace(``trace.task.<type>``),task 內若呼叫
  其他 tracer span 會自動掛在這條 trace 底下。透過 ``Tracer.start_trace``
  context manager 在 task coroutine 內開啟。
* **Trigger callback**:``on_complete`` 可選 coroutine,task 結束(無論 ok /
  fail / cancel)時 fire,對應 P1-4 trigger 子系統的「task 完成續推」用法。

## 對 LLM 暴露 callable 的安全性

LLM **不能任意執行 Python callable** — 開發者必須先 ``register_task_type``
把可被 LLM 啟動的 task type 註冊到全域 registry,LLM 只能傳 ``type="scrape_url"``
這種字串 key 才能觸發。對應 ``tools/task.py`` 的 ``register_task_type`` /
``task_start`` meta-tool 介面。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anila_agent.tracing import Tracer

logger = logging.getLogger(__name__)

# 預設 task timeout(秒)— 對齊 claude-code-src ``DEFAULT_TASK_TIMEOUT_MS = 5 min``。
DEFAULT_TASK_TIMEOUT_SECONDS: float = 300.0


class TaskState(str, Enum):
    """背景 task 的生命週期狀態。

    繼承 ``str`` 讓 ``task.state == "RUNNING"`` 與
    ``task.state == TaskState.RUNNING`` 都成立,方便 JSON 序列化。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """終態 — task 不會再轉換。"""
        return self in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        )


# task 完成時可選的 trigger callback signature。
# 收到 Task snapshot 後使用者自行決定要不要 queue_message 等。
OnCompleteCallback = Callable[["Task"], Awaitable[None]]


@dataclass
class Task:
    """單一 background task 的純資料容器。

    Attributes:
        task_id: 全域唯一識別碼(UUID hex)。LLM 在 task_status / task_wait 用
            這個值反查。
        description: 人類可讀說明(LLM 啟動 task 時提供)。
        state: 當前生命週期狀態。
        started_at: task 進入 ``RUNNING`` 的時間;``PENDING`` 階段為 ``None``。
        ended_at: task 結束時間;非終態為 ``None``。
        progress: 0.0 - 1.0 進度;task 內部可寫,manager 不會自動更新。
        result: 成功完成時的回傳值;失敗 / 取消時為 ``None``。
        error: 失敗 / timeout 時的錯誤描述;成功時為 ``None``。
        metadata: 自訂中繼資料(task type、用戶輸入、tracing trace_id 等)。
    """

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    description: str = ""
    state: TaskState = TaskState.PENDING
    started_at: datetime | None = None
    ended_at: datetime | None = None
    progress: float = 0.0
    result: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化為 dict(供 meta-tool JSON 回傳或 trace metadata 用)。"""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "state": self.state.value,
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class TaskManager:
    """管理一組 background task 的 lifecycle — start / status / wait / cancel / list。

    每個 task 跑在自己的 ``asyncio.Task``,manager 自帶:

    * **例外隔離**:task 內 raise 不會破壞 manager 或其他 task。
    * **timeout 預設 300s**:可在 ``start`` 時用 ``timeout_seconds`` 覆寫。
    * **tracing**:若給定 ``tracer``,每個 task 都在 ``trace.task.<type>`` 內跑。
    * **on_complete trigger**:task 結束時 fire callback,跟 P1-4 trigger
      子系統互通(把訊息 queue 進 session)。

    Attributes:
        tracer: 可選 P0-9 Tracer;若給定則每個 task 一條 trace。
        default_timeout_seconds: 未指定 ``timeout_seconds`` 時的預設值。
        on_complete: task 結束時 fire 的 callback。可選。
    """

    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        default_timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
        on_complete: OnCompleteCallback | None = None,
    ) -> None:
        """初始化空 task table。

        Args:
            tracer: P0-9 Tracer;每個 task 開一條 ``trace.task.<type>`` trace。
            default_timeout_seconds: 沒指定 ``timeout_seconds`` 時 fallback 值。
            on_complete: task 終態時 fire 的 callback(收 Task snapshot)。
        """
        self._tracer = tracer
        self._default_timeout = default_timeout_seconds
        self._on_complete = on_complete
        self._tasks: dict[str, Task] = {}
        self._asyncio_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    async def start(
        self,
        description: str,
        coro_factory: Callable[..., Awaitable[Any]],
        *args: Any,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """啟動一個新 background task。

        Args:
            description: 人類可讀說明(LLM 一定要提供)。
            coro_factory: 已驗證可執行的 async callable;
                ``register_task_type`` 註冊過的 callable 才能由 LLM 觸發,但本
                method 不檢查 — 由 ``tools/task.py`` 的 meta-tool 層把關。
            *args: 透傳給 coro_factory 的位置參數。
            timeout_seconds: 此 task 的 timeout(秒),覆寫 manager 預設。
                ``None`` 代表使用 manager ``default_timeout_seconds``。
            metadata: 自訂中繼資料(會被存進 ``Task.metadata``)。
            **kwargs: 透傳給 coro_factory 的關鍵字參數。

        Returns:
            task_id — LLM 之後用此 id 查 status / wait / cancel。
        """
        task = Task(
            description=description,
            state=TaskState.PENDING,
            metadata=dict(metadata) if metadata else {},
        )
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._default_timeout
        )
        async with self._lock:
            self._tasks[task.task_id] = task

        async_task = asyncio.create_task(
            self._run_task(task, coro_factory, args, kwargs, timeout),
            name=f"anila-task:{task.task_id}",
        )
        async with self._lock:
            self._asyncio_tasks[task.task_id] = async_task

        return task.task_id

    async def status(self, task_id: str) -> Task:
        """查詢 task 當前狀態(回傳 snapshot,呼叫端不應 mutate)。

        Raises:
            KeyError: ``task_id`` 不存在於 manager 內。
        """
        async with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        return task

    async def wait(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> Task:
        """等待 task 抵達終態(或 timeout 過期)。

        timeout **不取消 task** — 只表示「我等夠了」,task 仍在背景跑。

        Args:
            task_id: 要等的 task。
            timeout: 最長等待秒數;``None`` = 無限等待。

        Returns:
            Task: task 當前 snapshot(timeout 過期時可能還在 ``RUNNING``)。

        Raises:
            KeyError: ``task_id`` 不存在。
        """
        async with self._lock:
            async_task = self._asyncio_tasks.get(task_id)
            task = self._tasks.get(task_id)
        if task is None or async_task is None:
            raise KeyError(f"task not found: {task_id}")

        if async_task.done():
            return task

        try:
            await asyncio.wait_for(asyncio.shield(async_task), timeout=timeout)
        except asyncio.TimeoutError:
            # wait timeout 不影響 task 本身;只是這次 wait 放棄
            pass
        except asyncio.CancelledError:
            # task 被 cancel — snapshot 已更新,回傳即可
            pass
        except Exception:
            # task 本身 raise 已在 _run_task 內處理,這裡吞掉
            pass
        return task

    async def cancel(self, task_id: str) -> Task:
        """取消 task;若已終態則 no-op 回傳當前 snapshot。

        Args:
            task_id: 要取消的 task。

        Returns:
            Task: 取消後 / 已終態的 snapshot。

        Raises:
            KeyError: ``task_id`` 不存在。
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            async_task = self._asyncio_tasks.get(task_id)
        if task is None or async_task is None:
            raise KeyError(f"task not found: {task_id}")

        if task.state.is_terminal:
            return task

        if not async_task.done():
            async_task.cancel()
            # 等 _run_task finally 收尾 (state 會被改成 CANCELLED)
            try:
                await async_task
            except (asyncio.CancelledError, Exception):
                pass
        return task

    async def list_tasks(
        self,
        state_filter: TaskState | None = None,
    ) -> list[Task]:
        """列出所有 task,可依 state 過濾。

        Args:
            state_filter: 只回傳該 state 的 task;``None`` 回傳全部。

        Returns:
            按 task_id 排序的 list(穩定順序便於測試)。
        """
        async with self._lock:
            tasks = list(self._tasks.values())
        if state_filter is not None:
            tasks = [t for t in tasks if t.state == state_filter]
        return sorted(tasks, key=lambda t: t.task_id)

    # ------------------------------------------------------------------
    # 內部 — 實際 task lifecycle
    # ------------------------------------------------------------------

    async def _run_task(
        self,
        task: Task,
        coro_factory: Callable[..., Awaitable[Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout: float,
    ) -> None:
        """task 主執行迴圈 — 開 trace、跑 coro、設 state、fire on_complete。

        本 method 是「不會 raise」的設計 —— 所有例外都轉成 ``task.state=FAILED``,
        ``asyncio.CancelledError`` 則設為 ``CANCELLED``。manager 不會 propagate
        例外給 caller(``start`` 已 return task_id)。
        """
        task_type = str(task.metadata.get("type", "unknown"))
        trace_name = f"trace.task.{task_type}"

        # tracing trace 是 context manager — 若沒 tracer 就用 nullcontext
        trace_cm: Any
        if self._tracer is not None:
            trace_cm = self._tracer.start_trace(
                trace_name,
                metadata={
                    "task_id": task.task_id,
                    "task.description": task.description,
                    "task.type": task_type,
                },
            )
        else:
            trace_cm = _NullTrace()

        task.state = TaskState.RUNNING
        task.started_at = datetime.now(timezone.utc)

        try:
            with trace_cm as trace:
                if trace is not None and self._tracer is not None:
                    task.metadata["trace_id"] = trace.trace_id

                try:
                    result = await asyncio.wait_for(
                        coro_factory(*args, **kwargs),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    task.state = TaskState.FAILED
                    task.error = (
                        f"timeout: task exceeded {timeout}s without completing"
                    )
                except asyncio.CancelledError:
                    task.state = TaskState.CANCELLED
                    task.error = "cancelled"
                    # CancelledError 不再 re-raise — 已在 task.state 反映,
                    # 並由 cancel() / stop_all() 流程協同處理。
                except Exception as exc:
                    task.state = TaskState.FAILED
                    task.error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "background task %s (type=%s) raised; "
                        "captured into task.error",
                        task.task_id,
                        task_type,
                        exc_info=exc,
                    )
                else:
                    task.state = TaskState.COMPLETED
                    task.result = result
                    if task.progress < 1.0:
                        task.progress = 1.0
        finally:
            task.ended_at = datetime.now(timezone.utc)
            # fire on_complete — 例外吞掉,別影響 task state
            if self._on_complete is not None:
                try:
                    await self._on_complete(task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "on_complete callback for task %s raised; suppressed",
                        task.task_id,
                        exc_info=True,
                    )


class _NullTrace:
    """tracer 為 None 時的 no-op context manager(不開實際 trace)。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


__all__ = [
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "OnCompleteCallback",
    "Task",
    "TaskManager",
    "TaskState",
]
