# -*- coding: utf-8 -*-
"""app.modules.tasks — Task Service(任務中心核心)。

職掌(doc 02 §1、doc 10 §4 Slice 2):任務(tasks / task_runs)的建立與
生命週期、Source Snapshot 與 Citation 的編排,以及任務層級的
orchestration(可呼叫 CSP Data Plane / Knowledge / Studio / Launch
Gateway)。每個正式 task 必須有 trace_id。

邊界規則:其他程式碼只能 `from app.modules import tasks` 或
`from app.modules.tasks import ...`(package 根的公開介面),不得 import
本 package 子模組的內部實作。本 package 不得 import
`app.modules.policy` / `app.modules.launch` 內部,也不得 import `app.api`。

公開介面(Slice 2b-A):Task Service 七函式 + `/api/tasks` router。
"""

from app.modules.tasks.router import router
from app.modules.tasks.service import (
    create_task,
    ensure_task_access,
    finish_task_run,
    get_task,
    list_tasks,
    start_task_run,
    transition_task,
)

__all__ = [
    "create_task",
    "ensure_task_access",
    "finish_task_run",
    "get_task",
    "list_tasks",
    "router",
    "start_task_run",
    "transition_task",
]
