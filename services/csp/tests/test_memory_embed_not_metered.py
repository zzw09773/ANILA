"""Long-term memory embedding is a deliberate non-metered call.

``memory_service._embed`` used to bypass the proxy entirely — the deleted
docstring called it "a layer we don't need for an internal background job" —
and therefore wrote no ``token_usage`` rows. Routing it through
``proxy_request`` to share the query/document decision point silently turned
metering on: ``persist_turn`` embeds both halves of a turn, so an ordinary
chat turn started writing ~3 extra usage rows attributed to the user, and
every per-user and per-department figure in the pilot moved without anyone
choosing that.

The choice recorded here is **not metered** (``record_usage=False``), matching
the behaviour these numbers were collected under. If internal memory embedding
should ever be metered, it needs its own ``request_type`` so the rows can be
told apart from user-initiated calls — flipping the flag alone would make the
existing rows wrong rather than complete.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import memory_service
from app.services.platform_embedding import PlatformEmbedding


def _install_fake_embedder(monkeypatch, captured: dict):
    fake_model = SimpleNamespace(
        id=7,
        name="nv-embed",
        model_type="embedding",
        endpoint_url="http://nv-embed:8000/v1",
        api_version="v1",
        protocol="openai_compatible",
        api_key_secret_ref=None,
        is_platform_embedding=True,
        embedding_native_dim=3,
        is_active=True,
        display_name="nv-embed",
        is_internal=False,
    )
    monkeypatch.setattr(
        memory_service,
        "resolve_platform_embedding",
        lambda db: PlatformEmbedding(
            model=fake_model, native_dim=3, truncates=False
        ),
    )
    monkeypatch.setattr(
        memory_service,
        "truncate_embedding",
        lambda v, pad_from=None: list(v),
    )

    async def fake_proxy_request(**kwargs):
        captured["record_usage"] = kwargs.get("record_usage", True)
        captured["role"] = kwargs.get("embedding_input_role")
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    import app.services.proxy.service as proxy_svc

    monkeypatch.setattr(proxy_svc, "proxy_request", fake_proxy_request)


@pytest.mark.parametrize("role", ["query", "document"])
def test_memory_embed_asks_the_proxy_not_to_meter(monkeypatch, role):
    captured: dict = {}
    _install_fake_embedder(monkeypatch, captured)

    asyncio.run(
        memory_service._embed(MagicMock(), "hello", embedding_input_role=role)
    )

    assert captured["record_usage"] is False, (
        "記憶向量化被計量了 —— 一輪對話會多寫約 3 筆 token_usage,"
        "所有 per-user / per-department 用量數字都會被墊高"
    )
    assert captured["role"] == role


def test_the_no_metering_choice_is_written_down(monkeypatch):
    """A flag this consequential must carry its reasoning at the call site.

    Guards against the failure mode that produced this defect: the metering
    behaviour changed as a side effect of a refactor, with nothing in the code
    or the commit message saying it had.
    """
    src = inspect.getsource(memory_service._embed)
    assert "record_usage=False" in src
    assert "Not metered" in src


def test_proxy_honours_record_usage_false_on_the_triton_path(monkeypatch):
    """The flag must actually suppress the enqueue, not just be accepted.

    ``record_usage`` is threaded through several layers; a parameter that is
    passed but never read is the same fake control in a different costume.
    """
    from app.services.proxy import service as proxy_impl

    enqueued: list = []

    async def _count(*_a, **_k):
        enqueued.append(1)

    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setattr(proxy_impl, "enqueue_usage", _count)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _count)
    monkeypatch.setattr(proxy_impl, "_note_proxy_outcome", lambda **_k: None)
    monkeypatch.setattr(
        "app.services.triton_grpc.embed_texts",
        lambda *a, **k: [[0.0] * 4],
    )

    model = SimpleNamespace(
        id=42,
        name="nv-embed-v2",
        model_type="embedding",
        endpoint_url="grpc://172.16.120.35:9001",
        api_version="v1",
        protocol="triton_grpc",
        api_key_secret_ref=None,
        display_name="nv-embed-v2",
        is_internal=True,
        classification_ceiling=None,
        is_active=True,
    )

    def _call(record_usage: bool):
        return asyncio.run(
            proxy_impl.proxy_request(
                model=model,
                api_key_id=None,
                user_id=1,
                department_id=None,
                request_body={"model": "nv-embed-v2", "input": ["x"]},
                endpoint_path="/v1/embeddings",
                embedding_input_role="document",
                record_usage=record_usage,
            )
        )

    _call(False)
    assert enqueued == [], "record_usage=False 仍然寫了用量列"

    _call(True)
    assert len(enqueued) == 1, "record_usage=True 沒有寫用量列 —— 計量整條斷掉"
