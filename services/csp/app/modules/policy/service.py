# -*- coding: utf-8 -*-
"""Policy Engine — Slice 2b-B:裁決「紀錄」與 ceiling 純函式。

本 slice 只負責兩件事(完整規則引擎在 Slice 3 / 6):

1. :func:`record_decision` —— 把一次政策裁決 **append** 進
   ``policy_decisions``(doc 03 §5)。fail-closed:``action`` 九值、
   ``decision`` 三值、``actor_type`` 二值皆以封閉 enum 驗證,非法即
   ``ValueError``、不落任何列;deny 必附可解釋 ``reason``(doc 03 Done
   Criteria 4)。與 audit 的 fail-soft 不同,policy decision 是治理
   Done Criteria 資料 —— 寫不進去就讓例外浮上來,不吞。
2. :func:`evaluate_classification_ceiling` —— doc 08 §10 的判定式
   ``allow if task.level <= ceiling`` 純函式(無 ceiling = 不設限)。
   後續 slice 的規則引擎會用;本 slice 先落單元測試。

Append-only:本模組 **不提供** 任何 update / delete 介面;``policy_decisions``
只 INSERT(models/policy_decision.py 同一契約)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.policy_decision import PolicyDecision
from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyActorType,
    PolicyDecisionVerdict,
)

# actor_id 欄位是 Integer(users.id / service client / agent id 都是整數);
# 非數值的 actor 識別字(理論上不該出現)不丟資料 —— 原文塞進 metadata。
_ACTOR_ID_RAW_KEY = "actor_id_raw"


def _validate_enum(kind: str, value: str, enum_cls) -> str:
    """fail-closed 驗證:非法值拋 ``ValueError``(列出合法值)。"""
    try:
        return enum_cls(value).value
    except ValueError:
        allowed = [member.value for member in enum_cls]
        raise ValueError(
            f"非法的 {kind}:{value!r};合法值為 {allowed}"
        ) from None


def record_decision(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    decision: str,
    actor_type: str,
    actor_id: str,
    task_id: int | None = None,
    reason: str | None = None,
    matched_policy_ids: list[str] | None = None,
    policy_version: str = "r1",
    metadata: dict | None = None,
) -> PolicyDecision:
    """追加一筆政策裁決(append-only;doc 03 §5)。

    - ``action`` / ``decision`` / ``actor_type`` 非法 → ``ValueError``,
      不落任何列(fail-closed)。
    - ``decision == "deny"`` 必附非空 ``reason``(doc 03 Done Criteria 4:
      所有 deny 都有可解釋原因)。``matched_policy_ids`` 在 r1 紀錄階段
      允許空(規則引擎未建,hardcoded guard 沒有 policy id 可填)。
    - 立即 commit:裁決紀錄是治理帳,寫入即須持久,不搭 caller 的交易。
    - 不改寫、不刪除既有列;本模組沒有任何 mutator。
    """
    action_value = _validate_enum("action", action, PolicyAction)
    decision_value = _validate_enum("decision", decision, PolicyDecisionVerdict)
    actor_type_value = _validate_enum("actor_type", actor_type, PolicyActorType)

    if decision_value == PolicyDecisionVerdict.DENY.value and not (
        reason and reason.strip()
    ):
        raise ValueError(
            "policy deny 必須附上可解釋的 reason(doc 03 Done Criteria 4)"
        )

    # 不變式:不動 caller 的物件 —— metadata / matched_policy_ids 一律複本。
    metadata_json: dict | None = dict(metadata) if metadata is not None else None

    actor_id_value: int | None
    try:
        actor_id_value = int(str(actor_id).strip())
    except (TypeError, ValueError):
        actor_id_value = None
        metadata_json = dict(metadata_json or {})
        metadata_json.setdefault(_ACTOR_ID_RAW_KEY, actor_id)

    row = PolicyDecision(
        task_id=task_id,
        actor_type=actor_type_value,
        actor_id=actor_id_value,
        action=action_value,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision_value,
        reason=reason,
        matched_policy_ids=list(matched_policy_ids or []),
        policy_version=policy_version,
        metadata_json=metadata_json,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def evaluate_classification_ceiling(
    *, task_level: str, ceiling: str | None
) -> bool:
    """doc 08 §10 判定式:``allow if task.level <= ceiling``。

    純函式,無副作用。``ceiling is None`` = 該資源不設分類上限 → True。
    等級字串一律經 :meth:`ClassificationLevel.from_storage` 解析,
    未知值 fail-closed 拋 ``ValueError``(不得默默放行)。
    """
    level = ClassificationLevel.from_storage(task_level)
    if ceiling is None:
        return True
    return level <= ClassificationLevel.from_storage(ceiling)
