"""Batch raw reasoning into one short user-facing progress line.

The 64KB raw-reasoning persist path is unchanged. This module only
produces a small summary history. Failures return None — they must
never block the assistant body.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from sqlalchemy.orm import Session

from app.models.model_registry import ModelRegistry
from app.services.proxy.urls import join_upstream_path

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 40
MAX_HISTORY = 24
MAX_ADDED_CHARS = 4000
MIN_ADDED_CHARS = 40
_HTTP_TIMEOUT = 8.0

_SUMMARY_MODEL_NAME = os.environ.get("THINKING_SUMMARY_MODEL") or os.environ.get(
    "MEMORY_LLM_MODEL", "gemma4"
)

_ARBITRATION = re.compile(
    r"語言|正體|繁體中文|簡體|系統指令|system prompt|instruction|i should|i need|the user asked",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "你是對話進度播報員。根據模型新增的思考內容，用一句繁體中文向使用者說明"
    "它現在在做什麼。規則：只輸出一句話，最多 40 字；面向使用者，不要複述內心推理、"
    "不要提起語言指令、系統提示或翻譯仲裁；不要用英文；不要加引號或條列。"
)


def sanitize_summary(text: Optional[str]) -> Optional[str]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        end = raw.rfind("```")
        if end != -1:
            raw = raw[:end]
        raw = raw.strip()
    if not raw:
        return None
    first = raw.splitlines()[0].strip().strip("「」\"'")
    if len(first) < 4 or len(first) > MAX_SUMMARY_CHARS:
        return None
    if _ARBITRATION.search(first):
        return None
    if not first.endswith("。"):
        first = first.rstrip("．. ") + "。"
    return first


def append_summary(history: list | None, text: str, at: int | None = None) -> list:
    item = (text or "").strip()
    rows = list(history or [])
    if not item:
        return rows
    if rows and rows[-1].get("text") == item:
        return rows
    rows.append({"text": item, "at": at or 0})
    return rows[-MAX_HISTORY:]


def build_summarize_payload(
    *,
    model: str,
    added: str,
    previous: list | None,
) -> dict[str, Any]:
    prior = [
        str(item.get("text") or "").strip()
        for item in (previous or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    prior_block = "\n".join(f"- {line}" for line in prior[-MAX_HISTORY:]) or "（尚無）"
    chunk = (added or "")[-MAX_ADDED_CHARS:]
    user = (
        f"已播報：\n{prior_block}\n\n"
        f"新增思考（僅供你歸納進度，不要照抄）：\n{chunk}"
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 64,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _resolve_summary_target(db: Session) -> tuple[str, str] | None:
    try:
        row = (
            db.query(ModelRegistry)
            .filter(
                ModelRegistry.name == _SUMMARY_MODEL_NAME,
                ModelRegistry.model_type == "llm",
                ModelRegistry.is_active.is_(True),
            )
            .first()
        )
        if row is not None:
            return row.name, row.endpoint_url.rstrip("/")
    except Exception:
        logger.exception("thinking_summary: registry lookup failed")
        return None
    fallback = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.model_type == "llm", ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.id.asc())
        .first()
    )
    if fallback is None:
        return None
    return fallback.name, fallback.endpoint_url.rstrip("/")


async def summarize_reasoning_batch(
    db: Session,
    *,
    added: str,
    previous: list | None = None,
) -> Optional[str]:
    chunk = (added or "").strip()
    if len(chunk) < MIN_ADDED_CHARS:
        return None
    target = _resolve_summary_target(db)
    if target is None:
        return None
    model_name, base_url = target
    url = join_upstream_path(base_url, "/v1/chat/completions")
    try:
        validate_outbound_url(url)
    except UnsafeEndpointError:
        logger.warning("thinking_summary: endpoint failed SSRF guard")
        return None
    payload = build_summarize_payload(
        model=model_name, added=chunk, previous=previous
    )
    try:
        db.commit()
    except Exception:
        logger.exception("thinking_summary: commit before outbound failed")
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        content = (resp.json()["choices"][0]["message"].get("content") or "")
    except Exception:
        logger.warning("thinking_summary: LLM call failed", exc_info=True)
        return None
    return sanitize_summary(content)
