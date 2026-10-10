"""Agent 有效分類等級。

API、對話服務、知識庫分類都用同一條規則。放在 services，避免
``app.services`` / ``app.modules`` 回頭 import ``app.api``。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.models.agent import Agent
from app.schemas.contracts.classification import ClassificationLevel


def parse_stored_classification_level(raw: str | None) -> ClassificationLevel:
    """Parse a stored level; illegal DB values refuse with 422 (not 500)."""
    try:
        return ClassificationLevel.from_storage(raw or "無機密")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "預設分類等級無效，請改設為合法四級之一："
                "無機密、營業秘密、密、機密"
            ),
        ) from exc


def effective_agent_policy_level(agent: Agent) -> ClassificationLevel:
    """Effective policy level — same rule as ``proxy._agent_policy_level``.

    Stored ``default_classification_level``, floored at RESTRICTED(密) when
    the legacy ``requires_encryption`` flag is set. Writers that compare
    "current vs requested" must use this, not the raw stored column alone.
    """
    level = parse_stored_classification_level(
        getattr(agent, "default_classification_level", None)
    )
    if bool(getattr(agent, "requires_encryption", False)):
        level = ClassificationLevel.max_of(
            [level, ClassificationLevel.RESTRICTED]
        )
    return level
