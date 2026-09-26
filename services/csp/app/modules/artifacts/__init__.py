# -*- coding: utf-8 -*-
"""app.modules.artifacts — Artifact Service(Studio 產出契約持久化)。

職掌(doc 02 ArtifactJob、doc 01 Artifact/Version/Export、doc 08 §5/§10):
把 Studio 五類產出(slides / report / mindmap / infographic / datatable)的
job 生命週期與成品物件搬進 CSP DB —— doc 02 §8 blocker:「Studio restart 後
job 不應丟失」。負責 artifact_jobs 冪等 upsert 與狀態機、artifacts 的
binding 規則(必綁 task 或 source_snapshot,constitution §6)、artifact
版本、export_records 落地與治理 owner-scope 讀取。

邊界規則(independence 契約):其他程式碼只能 `from app.modules import
artifacts` 或 `from app.modules.artifacts import ...`,不得 import 本 package
子模組內部實作。本 package **不得** import `app.modules.tasks` /
`app.modules.policy` / `app.modules.launch`,也不得 import `app.api`。分類的
單向閂鎖(ClassificationEvent)與 PolicyDecision / 匯出 gate 屬 policy 核心,
由 orchestrator `app.api.artifacts` 呼叫 policy 完成;本 module 只做 DB 落地與
讀取(``inherited_level`` 只讀來源等級供 orchestrator 計算 max)。

公開介面(Slice 8a)。
"""

from app.modules.artifacts.service import (
    attach_collection_scope,
    create_artifact,
    create_version,
    ensure_artifact_access,
    get_artifact,
    inherited_level,
    list_artifacts,
    record_export,
    register_job,
    resolve_owner,
    sync_version_level,
    transition_job,
)

__all__ = [
    "attach_collection_scope",
    "create_artifact",
    "create_version",
    "ensure_artifact_access",
    "get_artifact",
    "inherited_level",
    "list_artifacts",
    "record_export",
    "register_job",
    "resolve_owner",
    "sync_version_level",
    "transition_job",
]
