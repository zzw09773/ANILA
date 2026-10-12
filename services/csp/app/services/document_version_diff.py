"""兩版知識文件的比對。

差異清單只由程式產生。模型若被叫來說明，只能解釋已經存在的差異編號；
說明對不到編號就丟掉，不會變成新的一筆修改。
"""

from __future__ import annotations

import logging
from typing import Any

from anila_core.diff.article_diff import (
    attach_model_output,
    compare_docx_bytes,
    explanation_request,
)

logger = logging.getLogger(__name__)

NOTE_SKIPPED_UNREADABLE = "文件讀不了，沒有請模型說明。"
NOTE_SKIPPED_EMPTY = "沒有可說明的差異編號，沒有請模型另做判斷。"
NOTE_UNAVAILABLE = "目前沒有可用的摘要模型。差異仍以程式比對為準。"
NOTE_FAILED = "模型沒有說明這些差異。卡片仍是程式比對的結果。"


class DocumentDiffQuota(Exception):
    """使用者額度擋下說明請求。訊息沿用額度服務的原文。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def compare_documents(before: bytes, after: bytes) -> dict[str, Any]:
    diff = compare_docx_bytes(before, after)
    diff["explanations"] = []
    diff["model_status"] = "not_requested"
    diff["model_note"] = ""
    return diff


async def explain_documents(db, user, before: bytes, after: bytes) -> dict[str, Any]:
    """重算差異後才請已登記的摘要模型說明。不接受呼叫端自帶的差異清單。"""
    diff = compare_docx_bytes(before, after)
    if not diff["readable"]:
        return _without_model(diff, "skipped", NOTE_SKIPPED_UNREADABLE)
    if not diff["changes"]:
        return _without_model(diff, "skipped", NOTE_SKIPPED_EMPTY)

    from app.services.internal_llm import InternalCompletionError, complete_chat
    from app.services.model_roles import resolve_role

    try:
        resolved = resolve_role(db, "summary")
    except Exception:
        logger.exception("document diff: summary role lookup failed")
        return _without_model(diff, "unavailable", NOTE_UNAVAILABLE)
    if resolved.status != "ok" or resolved.model is None:
        return _without_model(diff, "unavailable", NOTE_UNAVAILABLE)

    request = explanation_request(diff)
    body = {
        "model": resolved.model.name,
        "messages": request["messages"],
        "temperature": 0.1,
        "max_tokens": 1200,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        raw = await complete_chat(
            db,
            resolved.model,
            body,
            user_id=getattr(user, "id", None),
            department_id=getattr(user, "department_id", None),
            on_behalf_of_user=True,
        )
    except InternalCompletionError as exc:
        if exc.status_code == 429 and exc.quota_code == "quota_exceeded" and exc.quota_message:
            raise DocumentDiffQuota(exc.quota_message) from exc
        logger.info("document diff explain failed status=%s", exc.status_code)
        return _without_model(diff, "failed", NOTE_FAILED)

    attached = attach_model_output(
        diff,
        raw,
        allowed_ids=request["allowed_ids"],
        allowed_article_keys=request["allowed_article_keys"],
        prompt_truncated=request["truncated"],
    )
    logger.info(
        "document diff explain status=%s changes=%s explanations=%s",
        attached["model_status"],
        len(attached["changes"]),
        len(attached["explanations"]),
    )
    return attached


def _without_model(diff: dict[str, Any], status: str, note: str) -> dict[str, Any]:
    out = dict(diff)
    out["explanations"] = []
    out["model_status"] = status
    out["model_note"] = note
    return out
