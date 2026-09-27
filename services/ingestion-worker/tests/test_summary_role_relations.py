"""LLM 關聯用治理中心的摘要角色。沒設就略過，不猜模型名。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ingestion_worker.llm_relations import _call_llm, extract_and_resolve_llm


class _BoomPool:
    def acquire(self):
        raise AssertionError("unset summary role must not touch the database")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        enable_relation_llm=True,
        relation_llm_url="http://csp:8000/v1",
        relation_llm_api_key="sk-worker",
        relation_llm_timeout_seconds=5,
        relation_llm_verify_ssl=False,
        relation_llm_max_candidates=20,
        relation_llm_max_chars=1000,
    )


@pytest.mark.asyncio
async def test_unset_summary_role_skips_llm_relations(monkeypatch):
    async def unset(*, base_url, api_key):
        assert base_url == "http://csp:8000/v1"
        return None, "摘要模型尚未在治理中心設定"

    monkeypatch.setattr(
        "ingestion_worker.summary_role.resolve_summary_model", unset,
    )
    result = await extract_and_resolve_llm(
        _BoomPool(),
        collection_id=1,
        document_id=2,
        text="本文",
        run_id="run",
        settings=_settings(),
    )
    assert result == {"extracted": 0}


@pytest.mark.asyncio
async def test_llm_call_uses_the_summary_role_name(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "[]"}}]}

    class _Client:
        def __init__(self, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, path, json, headers):
            captured.update(json)
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await _call_llm(
        [{"role": "user", "content": "hi"}],
        _settings(),
        model="summary-from-console",
    )
    assert captured["model"] == "summary-from-console"
    assert "gemma4" not in captured["model"]
