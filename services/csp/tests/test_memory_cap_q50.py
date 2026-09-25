"""Q50-A memory destination policy and total block cap tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.api import models as models_api
from app.api import proxy as proxy_api
from app.middleware.caller import Caller
from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.services import memory_service, proxy_service
from app.services.proxy import service as proxy_impl
from app.services.memory_service import MemoryReadResult, RetrievedChunk
from tests.conftest import make_agent, make_model, make_user


_MEMORY_SENTINEL = "Q50_MEMORY_SENTINEL"


class _PostResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self):
        self.text = json.dumps(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "answer"}}
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 2,
                    "total_tokens": 4,
                },
            }
        )

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        return None


class _StreamResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        yield (
            'data: {"choices":[{"index":0,"delta":{"content":"answer"},'
            '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
            '"completion_tokens":2,"total_tokens":4}}'
        )
        yield ""
        yield "data: [DONE]"
        yield ""


class _CapturingClient:
    last_body: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).last_body = json
        return _PostResponse()

    def stream(self, method, url, json=None, headers=None):
        type(self).last_body = json
        return _StreamResponse()


@pytest.fixture(autouse=True)
def _isolated_upstream(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")
    _CapturingClient.last_body = None
    monkeypatch.setattr(
        proxy_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _CapturingClient(*args, **kwargs),
    )

    async def _no_usage(**kwargs):
        return None

    monkeypatch.setattr(proxy_impl, "enqueue_usage", _no_usage)
    monkeypatch.setattr(proxy_impl, "enqueue_usage_task_linked", _no_usage)
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)


def _body_text(body: dict) -> str:
    """記憶區塊在引用訊息裡，不在 system。回傳引用內文，長度上限才對得上。"""
    messages = body["messages"]
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        start = content.find("<quoted-memory>")
        end = content.find("</quoted-memory>")
        if start != -1 and end > start:
            return content[start + len("<quoted-memory>") : end].strip()
    raise AssertionError("記憶沒有放進不可遵循的引用訊息")


class _Request:
    def __init__(self, body: dict):
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


async def _call_chat(db: Session, caller_user, body: dict):
    response = await proxy_api.chat_completions(
        _Request(body),
        caller=Caller(user=caller_user, api_key_id=None),
        db=db,
    )
    if body.get("stream"):
        async for _chunk in response.body_iterator:
            pass
    return response


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.asyncio
async def test_registered_agent_outbound_payload_contains_no_memory(
    db: Session, monkeypatch, stream: bool
):
    """The actual agent request body must contain zero memory-derived text."""
    caller = make_user(db, username=f"q50-agent-caller-{stream}", role="admin")
    agent = make_agent(
        db,
        caller,
        name=f"q50-agent-{stream}",
        approval_status="approved",
    )

    async def _memory_if_called(*args, **kwargs):
        return MemoryReadResult(
            block=_MEMORY_SENTINEL,
            facts_count=1,
            chunks=[],
        )

    monkeypatch.setattr(memory_service, "build_memory_block", _memory_if_called)

    response = await _call_chat(
        db,
        caller,
        {
            "model": agent.name,
            "stream": stream,
            "messages": [{"role": "user", "content": "agent question"}],
        },
    )

    if not stream:
        assert response["choices"][0]["message"]["content"] == "answer"
    assert _CapturingClient.last_body is not None
    outbound = _CapturingClient.last_body
    assert _MEMORY_SENTINEL not in json.dumps(outbound, ensure_ascii=False)
    assert outbound["messages"] == [
        {"role": "user", "content": "agent question"}
    ]


def _oversized_rows():
    facts = [
        SimpleNamespace(key="fact.new", value="FACT_NEW_" + "n" * 2100),
        SimpleNamespace(key="fact.old", value="FACT_OLD_" + "o" * 2100),
    ]
    chunks = [
        RetrievedChunk(
            id=2,
            conversation_id=1,
            role="user",
            content="CHUNK_HIGH_" + "h" * 650,
            cosine=0.95,
            is_encrypted=False,
        ),
        RetrievedChunk(
            id=1,
            conversation_id=1,
            role="assistant",
            content="CHUNK_LOW_" + "l" * 650,
            cosine=0.90,
            is_encrypted=False,
        ),
    ]
    return facts, chunks


def test_total_cap_drops_whole_memory_items_with_deterministic_priority():
    """Facts and chunks overflow the cap without producing partial items."""
    facts, chunks = _oversized_rows()
    block = memory_service._format_block(facts, chunks, max_chunk_chars=1200)

    assert block is not None
    assert len(block) <= memory_service._MEMORY_BLOCK_MAX_CHARS
    assert "FACT_NEW_" + "n" * 2100 in block
    assert "FACT_OLD_" not in block
    assert "CHUNK_HIGH_" not in block
    assert "CHUNK_LOW_" not in block
    assert "過往相關討論" not in block


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_internal", [False, True], ids=["external-label", "internal-label"]
)
async def test_non_agent_model_outbound_payload_contains_bounded_memory(
    db: Session, monkeypatch, stream: bool, is_internal: bool
):
    """A non-agent model receives memory regardless of the mutable label."""
    caller = make_user(
        db,
        username=f"q50-model-caller-{stream}-{is_internal}",
        role="admin",
    )
    model = make_model(db, name=f"q50-model-{stream}-{is_internal}")
    model.is_internal = is_internal
    db.commit()
    facts, chunks = _oversized_rows()

    monkeypatch.setattr(
        memory_service,
        "get_user_facts",
        lambda *args, **kwargs: facts,
    )

    async def _retrieve(*args, **kwargs):
        return chunks

    monkeypatch.setattr(memory_service, "retrieve_relevant_chunks", _retrieve)

    response = await _call_chat(
        db,
        caller,
        {
            "model": model.name,
            "stream": stream,
            "messages": [{"role": "user", "content": "model question"}],
        },
    )

    if not stream:
        assert response["choices"][0]["message"]["content"] == "answer"
    assert _CapturingClient.last_body is not None
    outbound = _CapturingClient.last_body
    memory_text = _body_text(outbound)
    assert len(memory_text) <= memory_service._MEMORY_BLOCK_MAX_CHARS
    assert "FACT_NEW_" in memory_text
    assert "FACT_OLD_" not in memory_text
    assert "CHUNK_HIGH_" not in memory_text
    assert "CHUNK_LOW_" not in memory_text


@pytest.mark.parametrize("source_is_internal", [False, True])
@pytest.mark.asyncio
async def test_bulk_imported_label_does_not_change_memory_payload(
    db: Session, monkeypatch, source_is_internal: bool
):
    """A bulk-import-inherited label cannot change non-agent memory behavior."""
    caller = make_user(
        db,
        username=f"q50-bulk-caller-{source_is_internal}",
        role="admin",
    )
    source = make_model(db, name=f"q50-bulk-source-{source_is_internal}")
    source.is_internal = source_is_internal
    db.commit()

    imported_name = f"q50-bulk-imported-{source_is_internal}"
    created, _, _, _, _ = models_api._apply_bulk_import_entries(
        db=db,
        source=source,
        entries=[{"id": imported_name}],
    )
    assert [entry.name for entry in created] == [imported_name]
    imported = (
        db.query(ModelRegistry).filter(ModelRegistry.name == imported_name).one()
    )
    assert imported.is_internal is source_is_internal
    imported.is_active = True
    db.commit()

    monkeypatch.setattr(
        memory_service,
        "get_user_facts",
        lambda *args, **kwargs: [
            SimpleNamespace(key="fact.bulk", value="BULK_IMPORT_MEMORY")
        ],
    )

    async def _retrieve(*args, **kwargs):
        return []

    monkeypatch.setattr(memory_service, "retrieve_relevant_chunks", _retrieve)

    await _call_chat(
        db,
        caller,
        {
            "model": imported.name,
            "messages": [{"role": "user", "content": "bulk question"}],
        },
    )

    assert _CapturingClient.last_body is not None
    assert "BULK_IMPORT_MEMORY" in _body_text(_CapturingClient.last_body)


def test_memory_target_predicate_depends_only_on_registered_agent_identity():
    """Only a registered-agent target disables user-memory injection."""
    agent = Agent()

    assert proxy_api._target_allows_memory(None) is True
    assert proxy_api._target_allows_memory(agent) is False
