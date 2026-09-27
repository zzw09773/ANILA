"""關聯抽取用的語言模型來自治理中心的摘要角色。

問法與視覺角色相同：``GET /api/models/roles/summary``。沒設就回
(None, 訊息)，呼叫端略過 LLM 關聯，規則與相似度關聯照常。
"""
from __future__ import annotations

from ingestion_worker.vision_role import resolve_role_model

_UNSET = "摘要模型尚未在治理中心設定"


async def resolve_summary_model(*, base_url: str, api_key: str) -> tuple[str | None, str]:
    return await resolve_role_model(
        "summary",
        vision_url=base_url,
        api_key=api_key,
        unset_message=_UNSET,
    )
