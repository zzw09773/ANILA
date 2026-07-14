"""Unit tests for prompt_gen_service.generate_system_prompt's outbound HTTP call.

Locks three regressions found in review:

1. Endpoint URL construction must not double up the version segment when
   the registry row already carries the "/v1" convention, must tolerate a
   trailing slash, and must add "/v1" for a bare-host row — same
   normalization contract as memory_service._embed / _extract_facts.
2. Authorization must flow through the same per-model gateway-key helper
   the rest of the proxy stack uses (``resolve_model_gateway_key`` +
   ``_apply_gateway_auth``), not a hand-rolled read of the global env var —
   verified observably: setting ``settings.MODEL_GATEWAY_API_KEY`` (the
   shared helper's source) must produce a Bearer header.
3. An empty ``choices`` array in the upstream response must raise a clear
   RuntimeError instead of an unhandled IndexError.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.ingestion import IngestionCollection
from app.services import prompt_gen_service

from tests.conftest import make_model, make_user


def _make_collection(db: Session, owner_id: int, name: str = "規範知識庫") -> IngestionCollection:
    col = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model="nvidia/NV-embed-V2",
        embedding_fingerprint="sha256:" + "0" * 64,
        embedding_dim=4000,
        created_by=owner_id,
    )
    db.add(col)
    db.commit()
    db.refresh(col)
    return col


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Fake httpx.AsyncClient recording the endpoint URL + headers passed to .post()."""

    last_url: str = ""
    last_headers: dict = {}
    response_payload: dict = {
        "choices": [{"message": {"role": "assistant", "content": "產生的 system prompt"}}]
    }

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_url = url
        type(self).last_headers = dict(headers or {})
        return _FakeResponse(type(self).response_payload)


def _patch_client(monkeypatch, payload: dict | None = None):
    _FakeClient.last_url = ""
    _FakeClient.last_headers = {}
    _FakeClient.response_payload = payload or {
        "choices": [{"message": {"role": "assistant", "content": "產生的 system prompt"}}]
    }
    monkeypatch.setattr(
        prompt_gen_service.httpx, "AsyncClient", lambda *a, **k: _FakeClient(*a, **k)
    )
    # SSRF guard behavior is covered elsewhere (test_ssrf_call_time_guard.py /
    # test_models_ssrf.py); no-op it here so these tests are isolated to the
    # endpoint-construction / auth / empty-choices bugs under test.
    monkeypatch.setattr(prompt_gen_service, "validate_outbound_url", lambda *a, **k: None)


# ── endpoint URL normalization across base_url conventions ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint_url,expected",
    [
        ("http://gw.example:8000/v1", "http://gw.example:8000/v1/chat/completions"),
        ("http://gw.example:8000/v1/", "http://gw.example:8000/v1/chat/completions"),
        ("http://gw.example:8000", "http://gw.example:8000/v1/chat/completions"),
    ],
    ids=["with-v1", "with-v1-trailing-slash", "bare-host"],
)
async def test_generate_system_prompt_normalizes_endpoint_url(
    db, monkeypatch, endpoint_url, expected,
):
    # Each parametrize case gets its own fresh (function-scoped) SQLite db, so
    # a fixed username/model name across cases is safe — no cross-case reuse.
    owner = make_user(db, username="pg_owner")
    model = make_model(db, name="pg-llm")
    model.endpoint_url = endpoint_url
    model.is_router_primary = True
    db.commit()
    col = _make_collection(db, owner.id)
    _patch_client(monkeypatch)

    result = await prompt_gen_service.generate_system_prompt(
        db, col.id, "這是一個測試構想", owner,
    )

    assert result == "產生的 system prompt"
    assert _FakeClient.last_url == expected


# ── auth: must go through the shared gateway-key helper ─────────────────────


@pytest.mark.asyncio
async def test_generate_system_prompt_applies_gateway_auth_via_shared_helper(db, monkeypatch):
    """Regression: the old code read ``os.environ["MODEL_GATEWAY_API_KEY"]``
    directly, bypassing the per-model key resolution the rest of the proxy
    stack uses. Observable proxy for "uses the shared helper": patching
    ``settings.MODEL_GATEWAY_API_KEY`` (what ``resolve_model_gateway_key`` /
    ``_apply_gateway_auth`` read) must produce a Bearer header — a raw
    ``os.environ`` read would not see this patch."""
    owner = make_user(db, username="pg_owner_auth")
    make_model(db, name="pg-llm-auth")
    col = _make_collection(db, owner.id, name="認證知識庫")
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-pg-test")
    _patch_client(monkeypatch)

    await prompt_gen_service.generate_system_prompt(db, col.id, "測試構想", owner)

    assert _FakeClient.last_headers.get("Authorization") == "Bearer sk-pg-test"


# ── empty choices must raise a clear error, not IndexError ──────────────────


@pytest.mark.asyncio
async def test_generate_system_prompt_raises_on_empty_choices(db, monkeypatch):
    owner = make_user(db, username="pg_owner_empty")
    make_model(db, name="pg-llm-empty")
    col = _make_collection(db, owner.id, name="空回應知識庫")
    _patch_client(monkeypatch, payload={"choices": []})

    with pytest.raises(RuntimeError, match="LLM 回傳空內容"):
        await prompt_gen_service.generate_system_prompt(db, col.id, "測試構想", owner)
