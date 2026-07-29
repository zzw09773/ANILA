# -*- coding: utf-8 -*-
"""app.modules.policy — Policy Engine(政策引擎,含分類決策)。

職掌(SYSTEM-MAP §8):集中所有政策判定,包含四級分類決策
(無機密 < 營業秘密 < 密 < 機密)、單向閂鎖(classification
只能維持或升級,不得自動降級)、降級申請與主管批核流程,以及
PolicyDecision 的產出。分類等級契約型別在
`app.schemas.contracts.classification`。

邊界規則:其他程式碼只能 `from app.modules import policy` 或
`from app.modules.policy import ...`(package 根的公開介面),不得 import
本 package 子模組的內部實作。本 package 不得 import
`app.modules.tasks` / `app.modules.launch` 內部,也不得 import `app.api`。

公開面(append-only —— 永遠不外露 mutator;分類升級走單向閂鎖、降級
只有核准後的內部生效路徑):

- ``record_decision(db, *, action, resource_type, resource_id, decision,
  actor_type, actor_id, task_id=None, reason=None, matched_policy_ids=None,
  policy_version="r1", metadata=None) -> PolicyDecision``
- ``evaluate_classification_ceiling(*, task_level, ceiling) -> bool``
- ``apply_classification(db, *, resource_type, resource_id, new_level,
  actor_type, actor_id, reason, task_id=None, source="propagation")
  -> ClassificationEvent | None`` —— 單向閂鎖(Slice 3a)
- ``effective_level(db, *, resource_type, resource_id)
  -> ClassificationLevel``
- ``create_declassification_request(...)`` / ``decide_declassification(...)``
  —— doc 08 §7 變體 A 降級申請與裁決(Slice 3a)
- ``has_declassification_authority(db, user_id) -> bool`` ——
  「機密審批權責」查核 hook(與技術角色脫鉤)
- ``router`` —— GET /api/policy-decisions(admin tier 唯讀查詢)
"""

from app.modules.policy.router import router
from app.modules.policy.service import (
    apply_classification,
    create_declassification_request,
    decide_declassification,
    effective_level,
    evaluate_classification_ceiling,
    has_declassification_authority,
    record_decision,
)

__all__ = [
    "apply_classification",
    "create_declassification_request",
    "decide_declassification",
    "effective_level",
    "evaluate_classification_ceiling",
    "has_declassification_authority",
    "record_decision",
    "router",
]
