# -*- coding: utf-8 -*-
"""PolicyDecision 契約(doc 03 §5,Slice 2a)。

``action`` 九值與 ``decision`` 三值為封閉 enum(doc 03 逐字);DB 層
(app/models/policy_decision.py)存開放 String、append-only,本模組在
API 邊界 fail-closed 把關。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.contracts.classification import ClassificationLevel


class PolicyAction(str, enum.Enum):
    """doc 03 PolicyDecision.action 九值(逐字,順序照文件)。"""

    TASK_RUN = "task.run"
    MODEL_INVOKE = "model.invoke"
    AGENT_INVOKE = "agent.invoke"
    SERVICE_LAUNCH = "service.launch"
    ARTIFACT_EXPORT = "artifact.export"
    COLLECTION_READ = "collection.read"
    CLASSIFICATION_DOWNGRADE_REQUEST = "classification.downgrade_request"
    REGISTRY_CREATE = "registry.create"
    REGISTRY_APPROVE = "registry.approve"


class PolicyDecisionVerdict(str, enum.Enum):
    """doc 03 PolicyDecision.decision 三值。"""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyActorType(str, enum.Enum):
    """裁決主體:一般使用者或服務(router / worker / agent)。"""

    USER = "user"
    SERVICE = "service"


class PolicyDecisionOut(BaseModel):
    """PolicyDecision 讀出契約(from ORM;append-only,無更新契約)。

    doc 03 Done Criteria 4:所有 deny 必有可解釋原因(``reason`` +
    ``matched_policy_ids``)—— service 層強制。
    """

    id: int
    task_id: int | None = None
    actor_type: PolicyActorType
    actor_id: int | None = None
    action: PolicyAction
    resource_type: str
    resource_id: str | None = None
    decision: PolicyDecisionVerdict
    reason: str | None = None
    matched_policy_ids: list[str] = Field(default_factory=list)
    policy_version: str | None = None
    metadata_json: dict[str, Any] | None = None
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}
