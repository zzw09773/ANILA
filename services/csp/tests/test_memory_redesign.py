"""長期記憶改版：不存助理原文、只固定注入事實、閒置才萃取、需要時才搜摘要。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.platform_setting import set_setting
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.services import memory_service
from tests.conftest import login, make_user

_NO_TRUNCATION = 100_000


class _Fact:
    def __init__(self, key, value):
        self.key = key
        self.value = value


def test_format_block_drops_past_discussion_chunks():
    """固定注入的區塊只有事實與偏好，舊回答原文不得出現。"""
    from anila_core.memory.long_term import RetrievedChunk

    facts = [_Fact("unit", "雷達組")]
    chunks = [
        RetrievedChunk(
            id=1,
            conversation_id=9,
            role="assistant",
            content="LEAKED_ASSISTANT_ANSWER 隔離內網",
            cosine=0.99,
            is_encrypted=False,
        )
    ]
    block = memory_service._format_block(facts, chunks, max_chunk_chars=_NO_TRUNCATION)
    assert block is not None
    assert "雷達組" in block
    assert "過往相關討論" not in block
    assert "LEAKED_ASSISTANT_ANSWER" not in block
    assert "隔離內網" not in block


@pytest.mark.asyncio
async def test_build_memory_block_ignores_retrieved_raw_chunks(db, monkeypatch):
    user = make_user(db, username="mem-block")
    db.add(
        UserFact(
            id=1,
            user_id=user.id,
            key="unit",
            value="雷達組",
            confidence=1.0,
        )
    )
    db.commit()

    async def _retrieve(*args, **kwargs):
        from anila_core.memory.long_term import RetrievedChunk

        return [
            RetrievedChunk(
                id=4,
                conversation_id=2,
                role="assistant",
                content="RAW_OLD_ANSWER",
                cosine=0.95,
                is_encrypted=False,
            )
        ]

    monkeypatch.setattr(memory_service, "retrieve_relevant_chunks", _retrieve)
    result = await memory_service.build_memory_block(db, user.id, "幫我做一份報告")
    assert "雷達組" in (result.block or "")
    assert "RAW_OLD_ANSWER" not in (result.block or "")
    assert result.chunks == []


@pytest.mark.asyncio
async def test_persist_turn_does_not_store_or_extract(db, monkeypatch):
    """每一輪結束不再把助理回答向量化，也不立刻打萃取模型。"""
    Session = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(memory_service, "SessionLocal", Session)
    called = {"embed": 0, "extract": 0}

    async def fake_embed(*args, **kwargs):
        called["embed"] += 1
        return [0.1, 0.2], "embed", 2

    async def fake_extract(*args, **kwargs):
        called["extract"] += 1
        return [{"key": "unit", "value": "不該現在寫入", "confidence": 1.0}]

    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    monkeypatch.setattr(memory_service, "_extract_facts", fake_extract)

    await memory_service.persist_turn(
        user_id=1,
        conversation_id=1,
        user_message="我在雷達組",
        assistant_message="SECRET_ASSISTANT_TEXT",
        is_encrypted=False,
    )

    assert called == {"embed": 0, "extract": 0}
    stored = db.query(ConversationMemoryChunk).all()
    assert all("SECRET_ASSISTANT_TEXT" not in (row.content or "") for row in stored)


def test_failed_rescued_and_error_turns_are_not_extractable():
    ok = memory_service.turn_is_extractable(
        user_text="我在雷達組，偏好條列",
        assistant_text="好的，之後用條列。",
        assistant_metadata={"anila_stream": {"state": "complete"}},
    )
    failed = memory_service.turn_is_extractable(
        user_text="我在雷達組",
        assistant_text="半截",
        assistant_metadata={"anila_stream": {"state": "failed"}},
    )
    rescued = memory_service.turn_is_extractable(
        user_text="我在雷達組",
        assistant_text="整理後的答案",
        assistant_metadata={
            "anila_stream": {"state": "complete"},
            "rescue": {"reason": "reasoning_exhausted"},
        },
    )
    notice = memory_service.turn_is_extractable(
        user_text="幫我查",
        assistant_text="產生回應時發生錯誤，請稍後再試。",
        assistant_metadata={"anila_stream": {"state": "complete"}},
    )
    kb_notice = memory_service.turn_is_extractable(
        user_text="本次無法查詢院內規章知識庫（檢索失敗）",
        assistant_text="沒查到。",
        assistant_metadata=None,
    )
    assert ok is True
    assert failed is False
    assert rescued is False
    assert notice is False
    assert kb_notice is False


def _conv(db, user, title="t"):
    conv = Conversation(user_id=user.id, title=title, origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _pair(db, conv, user_text, assistant_text, *, metadata=None, when=None):
    user_msg = Message(
        conversation_id=conv.id,
        parent_id=conv.active_leaf_message_id,
        role="user",
        content=user_text,
        classification_level="無機密",
    )
    if when is not None:
        user_msg.created_at = when
    db.add(user_msg)
    db.flush()
    asst = Message(
        conversation_id=conv.id,
        parent_id=user_msg.id,
        role="assistant",
        content=assistant_text,
        metadata_=metadata,
        classification_level="無機密",
    )
    if when is not None:
        asst.created_at = when
    db.add(asst)
    db.flush()
    conv.active_leaf_message_id = asst.id
    db.commit()
    return asst


def test_refresh_is_idle_or_new_conversation_and_skips_bad_turns(db, monkeypatch):
    user = make_user(db, username="mem-idle")
    old = _conv(db, user, "old")
    recent = _conv(db, user, "recent")
    past = datetime.now(timezone.utc) - timedelta(minutes=30)
    now = datetime.now(timezone.utc)
    _pair(
        db,
        old,
        "我在雷達組",
        "失敗的回答",
        metadata={"anila_stream": {"state": "failed"}},
        when=past,
    )
    _pair(db, old, "我偏好條列", "好。", metadata={"anila_stream": {"state": "complete"}}, when=past)
    _pair(
        db,
        recent,
        "我在雷達組",
        "剛說完。",
        metadata={"anila_stream": {"state": "complete"}},
        when=now,
    )
    set_setting(db, "memory.idle_minutes", 10)
    set_setting(db, "memory.enabled", True)

    idle = memory_service.conversations_due(db, now=now, user_id=user.id, force=False)
    assert old.id in idle
    assert recent.id not in idle

    forced = memory_service.conversations_due(
        db,
        now=now,
        user_id=user.id,
        exclude_conversation_id=99,
        force=True,
    )
    assert recent.id in forced
    assert old.id in forced


@pytest.mark.asyncio
async def test_refresh_uses_summary_role_and_skips_when_unset(db, monkeypatch, caplog):
    user = make_user(db, username="mem-role")
    conv = _conv(db, user)
    _pair(
        db,
        conv,
        "我在雷達組，請用條列",
        "助理逐字：這段不該進摘要。" * 3,
        metadata={"anila_stream": {"state": "complete"}},
    )
    memory_service.reset_summary_role_warning()

    async def _no_model(*_args, **_kwargs):
        raise AssertionError("unset summary role must not call a model")

    from app.services.internal_llm import complete_chat as real_complete_chat

    monkeypatch.setattr("app.services.internal_llm.complete_chat", _no_model)
    with caplog.at_level(logging.WARNING, logger=memory_service.__name__):
        await memory_service.refresh_conversation(conv.id, db=db)
        await memory_service.refresh_conversation(conv.id, db=db)
    monkeypatch.setattr("app.services.internal_llm.complete_chat", real_complete_chat)
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "摘要模型" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert db.query(UserFact).filter(UserFact.user_id == user.id).count() == 0

    from app.models.model_registry import ModelRegistry
    from app.models.model_role import ModelRole

    row = ModelRegistry(
        name="summary-llm",
        display_name="summary",
        model_type="llm",
        endpoint_url="http://summary.test/v1",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.add(ModelRole(role="summary", model_id=row.id))
    db.commit()

    captured = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["payload"] = json

            class _Resp:
                status_code = 200
                headers = {"content-type": "application/json"}
                text = "{}"

                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": (
                                        '{"summary":"使用者要條列，並說明自己在雷達組。'
                                        '助理逐字：這段不該進摘要。助理逐字：這段不該進摘要。",'
                                        '"facts":[{"key":"unit","value":"雷達組","confidence":0.9},'
                                        '{"key":"claim","value":"助理逐字：這段不該進摘要。","confidence":0.8}]}'
                                    )
                                }
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }

            return _Resp()

    async def fake_embed(db, text_input, **kwargs):
        captured["embedded"] = text_input
        return [1.0, 0.0], "embed-test", 2

    import httpx

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "summary.test")
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    monkeypatch.setattr(memory_service, "_guard_outbound", lambda url: None)
    await memory_service.refresh_conversation(conv.id, db=db)

    assert captured["payload"]["model"] == "summary-llm"
    assert "summary.test" in captured["url"]
    summary = db.query(memory_service.ConversationSummary).filter_by(conversation_id=conv.id).one()
    assert "不該進摘要" not in summary.summary
    assert "條列" in summary.summary or "雷達組" in summary.summary
    facts = db.query(UserFact).filter(UserFact.user_id == user.id).all()
    assert [fact.value for fact in facts] == ["雷達組"]


@pytest.mark.asyncio
async def test_disabled_memory_omits_the_block_and_skips_refresh(db, monkeypatch):
    user = make_user(db, username="mem-off")
    db.add(UserFact(id=7, user_id=user.id, key="unit", value="雷達組", confidence=1))
    db.commit()
    set_setting(db, "memory.enabled", False)
    result = await memory_service.build_memory_block(db, user.id, "你好")
    assert result.block is None

    conv = _conv(db, user)
    _pair(db, conv, "我在雷達組", "好", metadata={"anila_stream": {"state": "complete"}})
    called = {"n": 0}

    async def _no_model(*_args, **_kwargs):
        called["n"] += 1
        return ""

    monkeypatch.setattr("app.services.internal_llm.complete_chat", _no_model)
    await memory_service.refresh_conversation(conv.id, db=db)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_summary_search_returns_the_prior_summary_not_raw_answers(db, monkeypatch):
    user = make_user(db, username="mem-search")
    other = make_user(db, username="mem-search-other")
    conv = _conv(db, user, "報告")
    other_conv = _conv(db, other, "別人")
    now = datetime.now(timezone.utc)

    async def fake_embed(db, text_input, **kwargs):
        if "報告" in text_input:
            return [1.0, 0.0], "embed-test", 2
        return [0.0, 1.0], "embed-test", 2

    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    memory_service.save_conversation_summary(
        db,
        user_id=user.id,
        conversation_id=conv.id,
        summary="上次報告結論是用條列",
        covered_message_id=1,
        embedding=[1.0, 0.0],
        source_model="embed-test",
        native_dim=2,
        is_encrypted=False,
        when=now,
    )
    memory_service.save_conversation_summary(
        db,
        user_id=other.id,
        conversation_id=other_conv.id,
        summary="別人的摘要不該被看到",
        covered_message_id=1,
        embedding=[1.0, 0.0],
        source_model="embed-test",
        native_dim=2,
        is_encrypted=False,
        when=now,
    )
    db.commit()
    hits = await memory_service.search_conversation_summaries(db, user.id, "延續上次報告")
    assert [hit.summary for hit in hits] == ["上次報告結論是用條列"]


def test_user_can_edit_and_delete_fact_and_delete_summary(client, db):
    from app.middleware.cookies import CSRF_COOKIE_NAME

    owner = make_user(db, username="mem-owner")
    make_user(db, username="mem-other")
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "mem-owner", "password": "password"},
    )
    assert login_resp.status_code == 200, login_resp.text
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    owner_token = login_resp.json()["access_token"]
    fact = UserFact(id=11, user_id=owner.id, key="unit", value="雷達組", confidence=1)
    db.add(fact)
    conv = _conv(db, owner)
    memory_service.save_conversation_summary(
        db,
        user_id=owner.id,
        conversation_id=conv.id,
        summary="上次要條列",
        covered_message_id=None,
        embedding=None,
        source_model=None,
        native_dim=None,
        is_encrypted=False,
        when=datetime.now(timezone.utc),
    )
    db.commit()
    db.expire_all()

    missing = client.put("/api/memory/facts/11", json={"value": "通訊組"})
    assert missing.status_code == 403
    edited = client.put(
        "/api/memory/facts/11",
        headers={"X-CSRF-Token": csrf},
        json={"value": "通訊組"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["value"] == "通訊組"

    listed = client.get("/api/memory/summaries")
    assert listed.status_code == 200, listed.text
    summary_id = listed.json()["items"][0]["id"]
    assert listed.json()["items"][0]["summary"] == "上次要條列"
    denied = client.delete(f"/api/memory/summaries/{summary_id}")
    assert denied.status_code == 403

    other_headers = {"Authorization": f"Bearer {login(client, 'mem-other')}"}
    assert client.delete("/api/memory/facts/11", headers=other_headers).status_code == 404
    assert client.delete(
        f"/api/memory/summaries/{summary_id}",
        headers=other_headers,
    ).status_code == 404
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    deleted = client.delete(f"/api/memory/summaries/{summary_id}", headers=owner_headers)
    assert deleted.status_code == 200, deleted.text

    db.expire_all()
    import asyncio

    block = asyncio.run(memory_service.build_memory_block(db, owner.id, "你好")).block or ""
    assert "雷達組" not in block
    assert "通訊組" in block
    gone = client.delete("/api/memory/facts/11", headers=owner_headers)
    assert gone.status_code == 200, gone.text
    db.expire_all()
    block = asyncio.run(memory_service.build_memory_block(db, owner.id, "你好")).block or ""
    assert "通訊組" not in block


def test_migration_wipes_memory_rows_and_adds_summaries():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0045_conversation_summaries.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "r1_0045"' in src
    assert 'down_revision: Union[str, None] = "r1_0044"' in src
    assert "DELETE FROM conversation_memory_chunks" in src
    assert "DELETE FROM user_facts" in src
    assert "conversation_summaries" in src
