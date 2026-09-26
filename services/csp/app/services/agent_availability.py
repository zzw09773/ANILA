# -*- coding: utf-8 -*-
"""底層模型下線時，把綁定的 agent 標成不可用並通知擁有者。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.handoff import Notification
from app.models.model_registry import ModelRegistry

# 與健康探測、核准狀態無關的原因碼。
BASE_MODEL_OFFLINE = "base_model_offline"
# 使用者呼叫這個 agent 時看到的句子。
AGENT_TEMPORARILY_UNAVAILABLE = "此助手暫時無法使用"


def mark_agents_base_model_offline(
    db: Session,
    model: ModelRegistry,
    *,
    clear_binding: bool = False,
) -> None:
    """綁定此模型的 agent 改為不可用，並通知尚未通知過的擁有者。

    ``clear_binding`` 用於硬刪：先清掉 ``base_model_id``，避免資料庫
    沒開外鍵時留下指向已刪列的 id。
    """
    from app.models.agent import Agent

    agents = (
        db.query(Agent)
        .filter(Agent.base_model_id == model.id)
        .all()
    )
    for agent in agents:
        already = agent.unavailable_reason == BASE_MODEL_OFFLINE
        agent.unavailable_reason = BASE_MODEL_OFFLINE
        if clear_binding:
            agent.base_model_id = None
        if already:
            continue
        db.add(
            Notification(
                user_id=agent.owner_user_id,
                type="agent_base_model_offline",
                title="助手暫時無法使用",
                body=(
                    f"助手「{agent.name}」的底層模型「{model.name}」已停用或刪除。"
                    "請重新選擇模型並送審。"
                ),
                payload={"agent_id": agent.id, "model_id": model.id},
            )
        )
