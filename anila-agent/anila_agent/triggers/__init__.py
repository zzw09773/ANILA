"""anila-agent triggers sub-package (P1-4)。

讓 agent session 在 active 期間可以被「外部事件」喚醒,把自動化能力推進
agent loop。對應 enhancement roadmap **P1-4 Triggers 子系統**。

## 三種預設 trigger

* :class:`PeriodicTrigger` — 每 N 秒呼叫 callback (健康檢查 / 心跳)。
* :class:`FileChangeTrigger` — 監看檔案 mtime/size,變動時觸發 (config /
  prompt 模板熱重載);不依賴 watchfiles,純 std-lib 輪詢 + 1 秒 debounce。
* :class:`DbChangeTrigger` — 定期跑 query func,result 變動時觸發
  (ComfyUI job 完成、KB 文件數變動)。

## 整合點

* **P1-3 SessionContext**:trigger callback 透過
  ``ctx.session.queue_message(content, role="system")`` 把訊息注入
  agent message queue,下一輪 turn 開始時 prepend。
* **P0-9 tracing**:每次 trigger fire 開
  `trigger.fire.<trigger.name>` span。
* **P0-5 hook taxonomy**:trigger 跑完後可以 fire Inspect / Decide hook
  (此 P1-4 deliver mechanism,實際接線由 runner 後續介接)。

## ANILA 平台典型用法

```python
from anila_agent.core.hook_context import SessionContext
from anila_agent.triggers import (
    DbChangeTrigger,
    FileChangeTrigger,
    PeriodicTrigger,
    TriggerManager,
)

session = SessionContext(session_id="...")
manager = TriggerManager(session=session, tracer=tracer)

# 1. studio job 完成自動續推
async def fetch_job_status():
    return await db.fetch_one("SELECT status FROM studio_jobs WHERE id = ?", job_id)

async def on_studio_job_change(ctx, prev, curr):
    if curr == "done":
        ctx.session.queue_message(f"[studio] job {job_id} 已完成,可以下一步")

manager.register(
    DbChangeTrigger(fetch_job_status, on_studio_job_change, interval_seconds=5)
)

# 2. KB 文件變動 reindex
async def count_kb_docs():
    return await db.fetch_val("SELECT COUNT(*) FROM kb_docs")

async def notify_reindex(ctx, prev, curr):
    ctx.session.queue_message(
        f"[kb] 文件數從 {prev} 變 {curr},建議 reindex"
    )

manager.register(
    DbChangeTrigger(count_kb_docs, notify_reindex, interval_seconds=30)
)

# 3. vLLM 健康檢查
async def check_vllm_health(ctx):
    if not await ping_vllm():
        ctx.session.queue_message("[health] vLLM 連線異常,請暫停 LLM 操作")

manager.register(PeriodicTrigger(60.0, check_vllm_health))

async with manager:  # 等同於 start_all() / stop_all()
    ...  # agent 跑 turn 期間 trigger 在背景 fire
```
"""

from anila_agent.triggers.base import Trigger, TriggerManager
from anila_agent.triggers.db_change import (
    DbChangeCallback,
    DbChangeTrigger,
    DbQueryFunc,
)
from anila_agent.triggers.file_change import FileChangeCallback, FileChangeTrigger
from anila_agent.triggers.periodic import PeriodicCallback, PeriodicTrigger

__all__ = [
    "DbChangeCallback",
    "DbChangeTrigger",
    "DbQueryFunc",
    "FileChangeCallback",
    "FileChangeTrigger",
    "PeriodicCallback",
    "PeriodicTrigger",
    "Trigger",
    "TriggerManager",
]
