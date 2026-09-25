"""平台內部 chat completion 必須帶模型列上的金鑰，失敗不可把使用者原文存成記憶。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_registry import ModelRegistry
from app.models.model_role import ModelRole
from app.models.platform_setting import set_setting
from app.models.token_usage import TokenUsage
from app.models.user_memory import (
    ConversationSummary,
    MemoryRefreshLease,
    MemoryTombstone,
    UserFact,
)
from app.services import memory_service
from app.services.service_token_envelope import encode_service_token_envelope
from app.services.thinking_summary import summarize_reasoning_batch
from app.services.usage_service import get_caller_usage
from tests.conftest import make_model, make_user

MODEL_KEY = "sk-summary-row-key"


def _allow_summary_host(monkeypatch) -> None:
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "summary.test")
    from app.config import settings

    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "", raising=False)


def _summary_model(db) -> ModelRegistry:
    row = ModelRegistry(
        name="qwen38-flash-next",
        display_name="摘要",
        model_type="llm",
        endpoint_url="http://summary.test:4000/v1",
        api_version="v1",
        protocol="openai_compatible",
        is_active=True,
        api_key_secret_ref=encode_service_token_envelope(MODEL_KEY),
    )
    db.add(row)
    db.commit()
    db.add(ModelRole(role="summary", model_id=row.id))
    db.commit()
    return row


def _conv(db, user) -> Conversation:
    conv = Conversation(user_id=user.id, title="新對話", origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _pair(db, conv, user_text: str, assistant_text: str) -> None:
    user_msg = Message(
        conversation_id=conv.id,
        parent_id=conv.active_leaf_message_id,
        role="user",
        content=user_text,
        classification_level="無機密",
    )
    db.add(user_msg)
    db.flush()
    asst = Message(
        conversation_id=conv.id,
        parent_id=user_msg.id,
        role="assistant",
        content=assistant_text,
        metadata_={"anila_stream": {"state": "complete"}},
        classification_level="無機密",
    )
    db.add(asst)
    db.flush()
    conv.active_leaf_message_id = asst.id
    db.commit()


class _Resp:
    def __init__(self, status_code: int, content: str):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = content
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self.status_code >= 400:
            return {"error": {"message": "Unauthorized"}}
        return {
            "choices": [{"message": {"content": self._content}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }


def _install_client(monkeypatch, captured: dict, *, status_code: int, content: str):
    class _Client:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["calls"] = captured.get("calls", 0) + 1
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["body"] = json
            return _Resp(status_code, content)

    # 記憶、思考摘要與 proxy 共用同一個 httpx 模組。
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _enable_memory(db) -> None:
    set_setting(db, "memory.enabled", True)


@pytest.mark.asyncio
async def test_refresh_sends_stored_model_key(db, monkeypatch):
    """模型列上的加密金鑰要變成 Authorization，不能裸打 endpoint。"""
    _allow_summary_host(monkeypatch)
    _enable_memory(db)
    user = make_user(db, username="internal-key")
    conv = _conv(db, user)
    _pair(db, conv, "我在雷達組，請用條列", "好的，之後用條列。")
    _summary_model(db)
    captured: dict = {}
    _install_client(
        monkeypatch,
        captured,
        status_code=200,
        content='{"summary":"使用者在雷達組，希望用條列。","facts":[]}',
    )

    async def fake_embed(*args, **kwargs):
        return [1.0, 0.0], "embed-test", 2

    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    await memory_service.refresh_conversation(conv.id, db=db)

    assert captured["headers"].get("Authorization") == f"Bearer {MODEL_KEY}"
    assert captured["url"].endswith("/v1/chat/completions")
    assert "summary.test:4000" in captured["url"]


@pytest.mark.asyncio
async def test_refresh_401_stores_nothing_and_defers_retry(db, monkeypatch):
    """401 或不可用的輸出不落摘要、不寫墓碑，下一輪閒置才再試。"""
    _allow_summary_host(monkeypatch)
    _enable_memory(db)
    user = make_user(db, username="internal-401")
    conv = _conv(db, user)
    user_words = "我在雷達組，請用條列"
    _pair(db, conv, user_words, "好的。")
    _summary_model(db)
    captured: dict = {}
    _install_client(
        monkeypatch,
        captured,
        status_code=401,
        content="Unauthorized",
    )

    await memory_service.refresh_conversation(conv.id, db=db)
    db.expire_all()

    assert captured["calls"] == 1
    assert db.query(ConversationSummary).count() == 0
    assert db.query(UserFact).filter(UserFact.user_id == user.id).count() == 0
    assert db.query(MemoryTombstone).count() == 0
    lease = db.get(MemoryRefreshLease, conv.id)
    assert lease is not None
    assert lease.claimed_until > int(time.time()) + 60

    await memory_service.refresh_conversation(conv.id, db=db)
    assert captured["calls"] == 1
    assert db.query(ConversationSummary).filter(
        ConversationSummary.summary.contains(user_words[:6])
    ).count() == 0


@pytest.mark.asyncio
async def test_thinking_summary_sends_stored_model_key(db, monkeypatch):
    """思考進度摘要走同一條內部呼叫，帶同一把模型金鑰。"""
    _allow_summary_host(monkeypatch)
    _summary_model(db)
    captured: dict = {}
    _install_client(
        monkeypatch,
        captured,
        status_code=200,
        content="正在整理雷達組的條列偏好",
    )

    out = await summarize_reasoning_batch(
        db, added="使用者要條列，並說自己在雷達組。" * 4, previous=[]
    )

    assert captured["headers"].get("Authorization") == f"Bearer {MODEL_KEY}"
    assert captured["url"].endswith("/v1/chat/completions")
    assert out == "正在整理雷達組的條列偏好。"


def test_platform_usage_is_not_billed_to_the_user(db):
    """usage_kind=platform 留在帳上，但不進使用者自己的用量。"""
    user = make_user(db, username="internal-bill")
    model = make_model(db, name="bill-model")
    now = datetime.now(timezone.utc)
    db.add_all([
        TokenUsage(
            user_id=user.id,
            model_id=model.id,
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
            request_timestamp=now,
            request_type="chat",
            usage_kind="inference",
        ),
        TokenUsage(
            user_id=user.id,
            model_id=model.id,
            prompt_tokens=30,
            completion_tokens=8,
            total_tokens=38,
            request_timestamp=now,
            request_type="internal",
            usage_kind="platform",
        ),
    ])
    db.commit()
    usage = get_caller_usage(db, user_id=user.id, range_key="24h")
    assert usage["total_tokens"] == 14
    assert usage["requests"] == 1
    assert all(item["kind"] != "internal" for item in usage["by_kind"])


def test_wipe_migration_deletes_conversation_summaries():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0047_wipe_conversation_summaries.py"
    )
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert 'revision: str = "r1_0047"' in text
    assert 'down_revision: Union[str, None] = "r1_0046"' in text
    assert "DELETE FROM conversation_summaries" in text


@pytest.mark.asyncio
async def test_thinking_summary_is_billed_to_the_caller(db, monkeypatch):
    """思考摘要是使用者可直接呼叫的端點，用量記在該使用者，不是 platform。"""
    from app.services import internal_llm

    seen: dict = {}

    async def fake_complete_chat(db_, model, payload, **kwargs):
        seen.update(kwargs)
        return "整理中"

    monkeypatch.setattr(internal_llm, "complete_chat", fake_complete_chat)
    _allow_summary_host(monkeypatch)
    _summary_model(db)

    await summarize_reasoning_batch(db, added="使用者要條列。" * 8, previous=[])

    assert seen.get("on_behalf_of_user") is True


def test_upstream_error_body_is_redacted_before_logging():
    """上游錯誤本文回顯金鑰時，寫進 log 前要遮蔽。"""
    from app.services.proxy.service import _redact_upstream_text

    body = (
        '{"error":"bad key","echo":{"Authorization":"Bearer sk-summary-row-key"},'
        '"other":"sk-abcdefghijkl","hdr":"bearer eyJhbGciOiJIUzI1NiJ9.x.y"}'
    )
    out = _redact_upstream_text(body, {"Authorization": f"Bearer {MODEL_KEY}"})

    assert MODEL_KEY not in out
    assert "sk-abcdefghijkl" not in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "bad key" in out
