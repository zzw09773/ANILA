# -*- coding: utf-8 -*-
"""app.modules.policy — Policy Engine(政策引擎,含分類決策)。

職掌(doc 02 §1、doc 08):集中所有政策判定,包含五級分類決策
(無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密)、單向閂鎖(classification
只能維持或升級,不得自動降級)、降級申請與主管批核流程,以及
PolicyDecision 的產出。分類等級契約型別在
`app.schemas.contracts.classification`。

邊界規則:其他程式碼只能 `from app.modules import policy` 或
`from app.modules.policy import ...`(package 根的公開介面),不得 import
本 package 子模組的內部實作。本 package 不得 import
`app.modules.tasks` / `app.modules.launch` 內部,也不得 import `app.api`。

公開面(Slice 2b-B,recording only;append-only —— 永遠不外露 mutator):

- ``record_decision(db, *, action, resource_type, resource_id, decision,
  actor_type, actor_id, task_id=None, reason=None, matched_policy_ids=None,
  policy_version="r1", metadata=None) -> PolicyDecision``
- ``evaluate_classification_ceiling(*, task_level, ceiling) -> bool``
- ``router`` —— GET /api/policy-decisions(admin tier 唯讀查詢)
"""

from app.modules.policy.router import router
from app.modules.policy.service import (
    evaluate_classification_ceiling,
    record_decision,
)

__all__ = [
    "evaluate_classification_ceiling",
    "record_decision",
    "router",
]
