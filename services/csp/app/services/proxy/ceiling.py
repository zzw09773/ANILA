# -*- coding: utf-8 -*-
"""Slice 6a — classification ceiling check before a model gateway call.

doc 04 §5 / §8:``allow if task.classification_level <= model.classification_ceiling``。
現況機制(``requires_encryption`` + 單向 conversation latch)從不 deny 出向
呼叫;本模組補上 doc 04 §8「Classification ceiling check before outbound
call」硬要求 —— 在 CSP Model Gateway 把請求送往上游模型 **之前** 判定,
違反即 403 zh-TW、記一筆 ``PolicyDecision(action=model.invoke, decision=deny)``
且 **不** 發出向呼叫。

Effective level 決定順序(doc 04 §5 / doc 08 §4):
- task-linked:讀 Task 的 effective level(呼叫端已把 conversation 等級
  propagate 到 task)。
- 無 task 但有 latched conversation:讀 conversation 的 effective level。
- 皆無:``無機密``(fail-safe 起點,唯一不設限的情況)。

PolicyDecision 落列規則(本 slice 拍板,見任務 Deliverable 5):
- **deny**:一律記(task-linked 與 legacy 皆記);doc 03 Done Criteria 4
  要求 deny 必附可解釋 reason。
- **allow**:**僅 task-linked 記**。legacy(task-less)/v1 chat 量大,每次 allow
  都落列會灌爆 ``policy_decisions``;legacy allow 不落列(與 usage 的
  ``legacy_runtime_call`` 標記精神一致 —— 治理帳只對進 Task 主脊椎的流量
  完整記錄)。

模組邊界:``app.modules.policy`` 為 module-boundary package,call-time import
其 package 根公開面(``effective_level`` / ``evaluate_classification_ceiling`` /
``record_decision``),不 import 其內部子模組。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from anila_contracts import Classification as ClassificationLevel
from app.schemas.contracts.policy import PolicyAction, PolicyDecisionVerdict
from app.services.proxy.task_link import TaskRunContext, finalize_task_run

logger = logging.getLogger("app.services.proxy_service")


def _effective_task_level(
    db,
    *,
    task_ctx: Optional[TaskRunContext],
    conv_id_int: Optional[int],
) -> ClassificationLevel:
    """doc 04 §5 的 task effective level（fail-closed 讀取,查無 → 無機密）。"""
    from app.modules.policy import effective_level

    if task_ctx is not None:
        try:
            return effective_level(
                db, resource_type="task", resource_id=str(task_ctx.task_id)
            )
        except ValueError:
            return ClassificationLevel.UNCLASSIFIED
    if conv_id_int is not None:
        try:
            return effective_level(
                db, resource_type="conversation", resource_id=str(conv_id_int)
            )
        except ValueError:
            return ClassificationLevel.UNCLASSIFIED
    return ClassificationLevel.UNCLASSIFIED


def _enforce_ceiling(
    db,
    *,
    target,
    caller,
    task_ctx: Optional[TaskRunContext],
    conv_id_int: Optional[int],
    action: str,
    resource_type: str,
    target_label: str,
) -> None:
    """Shared ceiling gate for model.invoke and agent.invoke."""
    from app.modules.policy import evaluate_classification_ceiling, record_decision

    level = _effective_task_level(db, task_ctx=task_ctx, conv_id_int=conv_id_int)
    level_str = level.to_storage()
    actor_id = str(getattr(caller.user, "id", "") or "")
    task_id = task_ctx.task_id if task_ctx is not None else None
    resource_id = str(getattr(target, "id", "") or "")
    target_name = getattr(target, "name", "?")

    raw_ceiling = getattr(target, "classification_ceiling", None)
    try:
        if not isinstance(raw_ceiling, str) or not raw_ceiling.strip():
            raise ValueError("classification ceiling is NULL or empty")
        ceiling = ClassificationLevel.from_storage(raw_ceiling).to_storage()
    except ValueError as exc:
        reason = (
            f"{target_label}「{target_name}」分類上限資料無效，"
            "依 Gate 2 fail-closed 拒絕出向呼叫"
        )
        record_decision(
            db,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=PolicyDecisionVerdict.DENY.value,
            actor_type="user",
            actor_id=actor_id,
            task_id=task_id,
            reason=reason,
            metadata={"classification_state": type(raw_ceiling).__name__},
        )
        if task_ctx is not None:
            finalize_task_run(
                task_ctx.task_run_id,
                "failed",
                error={"code": "classification_ceiling", "message": reason},
            )
        raise HTTPException(status_code=403, detail=reason) from exc

    allowed = evaluate_classification_ceiling(task_level=level_str, ceiling=ceiling)

    if not allowed:
        reason = (
            f"任務分類等級「{level_str}」超過{target_label}「{target_name}」"
            f"分類上限「{ceiling}」,依 doc 04 §5 拒絕出向呼叫"
        )
        record_decision(
            db,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=PolicyDecisionVerdict.DENY.value,
            actor_type="user",
            actor_id=actor_id,
            task_id=task_id,
            reason=reason,
        )
        # task-linked:已開的 run 收尾為 failed(deny 前 begin_task_run 已起 run)。
        if task_ctx is not None:
            finalize_task_run(
                task_ctx.task_run_id,
                "failed",
                error={"code": "classification_ceiling", "message": reason},
            )
        raise HTTPException(status_code=403, detail=reason)

    # pass:僅 task-linked 記 allow(避免 legacy 灌爆 policy_decisions)。
    if task_ctx is not None:
        record_decision(
            db,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=PolicyDecisionVerdict.ALLOW.value,
            actor_type="user",
            actor_id=actor_id,
            task_id=task_id,
        )


def enforce_model_ceiling(
    db,
    *,
    model,
    caller,
    task_ctx: Optional[TaskRunContext],
    conv_id_int: Optional[int],
) -> None:
    """出向前分類 ceiling 把關。違反 → 403 + deny 列 + 不發出向。

    每個 model 都必須有顯式 ``classification_ceiling``。NULL、空字串或未知
    值視為損壞的治理狀態，會記 deny、回 403，且不發出向呼叫。合法 ceiling
    pass 時僅 task-linked 記 allow(避免 legacy 灌爆);deny 一律記錄。
    """
    _enforce_ceiling(
        db,
        target=model,
        caller=caller,
        task_ctx=task_ctx,
        conv_id_int=conv_id_int,
        action=PolicyAction.MODEL_INVOKE.value,
        resource_type="model",
        target_label="模型",
    )


def enforce_agent_ceiling(
    db,
    *,
    agent,
    caller,
    task_ctx: Optional[TaskRunContext],
    conv_id_int: Optional[int],
) -> None:
    """Agent dispatch 前分類 ceiling 把關。違反 → 403 + deny 列 + 不 dispatch."""
    _enforce_ceiling(
        db,
        target=agent,
        caller=caller,
        task_ctx=task_ctx,
        conv_id_int=conv_id_int,
        action=PolicyAction.AGENT_INVOKE.value,
        resource_type="agent",
        target_label="Agent",
    )
