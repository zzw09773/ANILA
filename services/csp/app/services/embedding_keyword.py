"""Optional summary-model rerank of keyword-fallback candidates."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from anila_core.embeddings.keyword import order_by_indexes, parse_rerank_indexes

logger = logging.getLogger(__name__)

_UNHEALTHY = frozenset({"unhealthy", "disabled", "down"})


async def maybe_rerank(db: Session, user, query: str, hits: list):
    """Reorder ``hits`` when the summary model answers. Keep the original order otherwise."""
    if len(hits) < 2:
        return hits
    try:
        from app.services.memory_service import _summary_model
        from app.services.proxy.service import proxy_request, resolve_proxy_tuning
        from app.services.proxy.snapshot import snapshot_model

        model = _summary_model(db)
        if model is None:
            return hits
        if getattr(model, "health_status", None) in _UNHEALTHY:
            return hits
        raw_ver = getattr(model, "api_version", None)
        api_version = raw_ver if raw_ver in ("v1", "v2") else "v1"
        snapshot = snapshot_model(model)
        snapshot.api_version = api_version
        if not getattr(snapshot, "protocol", None):
            snapshot.protocol = "openai_compatible"
        if not getattr(snapshot, "model_type", None):
            snapshot.model_type = "llm"
        numbered = "\n".join(
            f"{index}. {(getattr(getattr(hit, 'chunk', None), 'content', '') or '')[:180]}"
            for index, hit in enumerate(hits, start=1)
        )
        prompt = (
            "依與問題的相關度由高到低排列全部編號。"
            "每個編號恰好出現一次，只回編號，以逗號分隔。\n"
            f"問題：{query}\n{numbered}"
        )
        tuning = resolve_proxy_tuning(db)
        db.commit()
        data = await proxy_request(
            model=snapshot,
            api_key_id=None,
            user_id=getattr(user, "id", 0) or 0,
            department_id=getattr(user, "department_id", None),
            request_body={
                "model": model.name,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            endpoint_path=f"/{api_version}/chat/completions",
            record_usage=False,
            tuning=tuning,
        )
        text = data["choices"][0]["message"]["content"]
        indexes = parse_rerank_indexes(text, len(hits))
        if not indexes:
            return hits
        return order_by_indexes(hits, indexes)
    except Exception:
        logger.info("keyword rerank skipped", exc_info=True)
        return hits
