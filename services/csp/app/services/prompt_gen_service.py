"""產生 agent 領域 system prompt。

dev 在開發者 guide 頁選一個 collection + 輸入初始構想 → 這裡抽該 collection 幾筆
文件標題當 grounding，組 meta-prompt 請平台「主 LLM」(model_registry 的
is_router_primary / 第一個 active llm) 產生一份可直接貼進 anila-agent
``prompts/system.md`` 的領域 system prompt。

LLM 呼叫沿用 memory_service 的模式（registry 解析 endpoint + SSRF guard + httpx
POST /v1/chat/completions），gateway 模式自動帶 MODEL_GATEWAY_API_KEY。
"""

from __future__ import annotations

import logging
import os

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from sqlalchemy.orm import Session

from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.services.proxy.urls import join_upstream_path

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 120.0
_MAX_IDEAS_CHARS = 4000
_SAMPLE_DOCS = 8
_MAX_TOKENS = 2048

_META_SYSTEM_PROMPT = (
    "你是設計 RAG agent「系統提示（system prompt）」的專家。根據開發者的構想與該知識庫的"
    "實際內容，產生一份精簡、可直接使用的繁體中文 system prompt。要求：明確界定 agent 的"
    "角色與領域範圍、要求嚴格依據檢索到的知識庫內容回答並標註來源、知識庫沒有的內容要明說"
    "不知道而非杜撰、語氣專業簡潔。只輸出 system prompt 本文，不要任何前後說明、不要 "
    "markdown code fence。"
)


def _resolve_primary_llm(db: Session) -> tuple[str, str]:
    """回 (model_name, endpoint_url)：registry 內 is_router_primary 優先、否則第一個 active llm。"""
    row = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.model_type == "llm", ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.is_router_primary.desc(), ModelRegistry.id.asc())
        .first()
    )
    if row is None:
        raise RuntimeError("model_registry 內沒有可用的 LLM（is_active 的 llm）")
    return row.name, row.endpoint_url


def _strip_fence(text: str) -> str:
    """reasoning 模型有時包 ```fence；剝掉只留本文。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        end = t.rfind("```")
        if end != -1:
            t = t[:end]
    return t.strip()


async def generate_system_prompt(
    db: Session, collection_id: int, ideas: str, user
) -> str:
    """抽 collection 文件當 grounding，請主 LLM 產生領域 system prompt。

    raise：``LookupError``(collection 不存在) / ``PermissionError``(非 owner/admin) /
    ``RuntimeError``(無可用 LLM、SSRF guard、空回應、上游錯誤)。
    """
    col = (
        db.query(IngestionCollection)
        .filter(IngestionCollection.id == collection_id)
        .first()
    )
    if col is None:
        raise LookupError("collection 不存在")

    is_admin = getattr(user, "role", "") in ("owner", "admin")
    if col.created_by != getattr(user, "id", None) and not is_admin:
        raise PermissionError("無權存取此 collection")

    titles = [
        (norm or fname)
        for fname, norm in (
            db.query(IngestionDocument.filename, IngestionDocument.normalized_title)
            .filter(IngestionDocument.collection_id == collection_id)
            .order_by(IngestionDocument.id.asc())
            .limit(_SAMPLE_DOCS)
            .all()
        )
    ]

    model_name, base_url = _resolve_primary_llm(db)
    endpoint = join_upstream_path(base_url, "/v1/chat/completions")
    try:
        validate_outbound_url(endpoint)
    except UnsafeEndpointError as exc:
        raise RuntimeError(f"LLM 端點未通過 SSRF guard：{exc}") from exc

    sample_block = "\n".join(f"- {t}" for t in titles) or "（此 collection 尚無文件）"
    user_msg = (
        f"知識庫名稱：{col.name}\n"
        f"知識庫說明：{col.description or '（無）'}\n"
        f"知識庫文件（取樣 {len(titles)} 筆）：\n{sample_block}\n\n"
        f"開發者構想：\n{ideas.strip()[:_MAX_IDEAS_CHARS]}\n\n"
        "請依上述產生這個 agent 的領域 system prompt。"
    )

    gateway_key = os.environ.get("MODEL_GATEWAY_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {gateway_key}"} if gateway_key else {}
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": _META_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": _MAX_TOKENS,
    }

    # Release the pooled connection before the outbound LLM call (timeout 120s).
    # All DB reads above are done; nothing after this await needs this Session.
    db.commit()

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"].get("content") or ""
    except httpx.HTTPError as exc:
        logger.warning("prompt_gen: LLM 呼叫失敗: %s", exc)
        raise RuntimeError(f"呼叫 LLM 失敗：{exc}") from exc

    result = _strip_fence(content)
    if not result:
        raise RuntimeError("LLM 回傳空內容（reasoning 模型可能耗光 token，請再試一次）")
    return result
