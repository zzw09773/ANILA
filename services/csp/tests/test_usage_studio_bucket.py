# -*- coding: utf-8 -*-
"""用量頁要分得出「簡報製作」（2026-09-02 擁有者問「製作簡報的用量有沒有被統計」）。

有統計，但 Studio 走使用者身分、沒有 API 金鑰，全被算進「對話介面」。現在：
* Studio 工作權杖（或 anila-studio 服務憑證）→ ``token_usage.request_type='studio'``。
  只帶 ``X-ANILA-Request-Source: studio`` 仍算對話。
* 用量摘要多一格 ``studio_requests``，``web_ui_requests`` 不再把它算進去。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api import proxy as proxy_api
from app.middleware.caller import Caller
from app.models.token_usage import TokenUsage
from app.services import proxy_service, usage_writer
from app.services.usage_service import get_usage_summary
from tests.conftest import make_model, make_user
from tests.test_proxy_nonstream_usage import _AgentClient, _Request


def test_summary_splits_studio_from_web_ui(db: Session):
    user = make_user(db, username="studio-usage-user")
    model = make_model(db, name="studio-usage-model")
    now = datetime.now(timezone.utc)
    db.add(TokenUsage(api_key_id=None, user_id=user.id, model_id=model.id, prompt_tokens=10, completion_tokens=5, total_tokens=15, request_timestamp=now, request_type="chat"))
    db.add(TokenUsage(api_key_id=None, user_id=user.id, model_id=model.id, prompt_tokens=100, completion_tokens=50, total_tokens=150, request_timestamp=now, request_type="studio"))
    db.add(TokenUsage(api_key_id=None, user_id=user.id, model_id=model.id, prompt_tokens=100, completion_tokens=50, total_tokens=150, request_timestamp=now, request_type="studio"))
    db.commit()
    s = get_usage_summary(db, range_key="24h")
    assert s["studio_requests"] == 2
    assert s["studio_tokens"] == 300
    assert s["web_ui_requests"] == 1
    assert s["total_requests"] == 3


@pytest.mark.asyncio
async def test_studio_source_header_tags_the_usage_row(db: Session, db_engine, monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", lambda *a, **kw: _AgentClient(*a, **kw))
    monkeypatch.setattr(proxy_service, "build_agent_headers", lambda **kwargs: {})
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)
    monkeypatch.setattr(usage_writer, "SessionLocal", sessionmaker(bind=db_engine, expire_on_commit=False))
    monkeypatch.setattr(usage_writer, "_usage_queue", None)
    caller = make_user(db, username="studio-tag-caller", role="admin")
    model = make_model(db, name="studio-tag-model")

    req = _Request({"model": model.name, "stream": False, "messages": [{"role": "user", "content": "簡報"}]})
    req.headers = {"X-ANILA-Request-Source": "studio"}
    await proxy_api.chat_completions(req, caller=Caller(user=caller, api_key_id=None), db=db)

    queue = usage_writer.get_usage_queue()
    await usage_writer._flush_batch([queue.get_nowait()])
    db.expire_all()
    row = db.query(TokenUsage).filter(TokenUsage.user_id == caller.id).one()
    assert row.request_type == "chat"

    from app.services.studio_job_token import issue_studio_job_token

    token = issue_studio_job_token(
        user_id=caller.id,
        job_id="deck-usage",
        classification="無機密",
        token_version=int(caller.token_version or 0),
        db=db,
    )
    job = _Request({"model": model.name, "stream": False, "messages": [{"role": "user", "content": "簡報"}]})
    job.headers = {"Authorization": f"Bearer {token}"}
    await proxy_api.chat_completions(job, caller=Caller(user=caller, api_key_id=None), db=db)
    await usage_writer._flush_batch([queue.get_nowait()])
    db.expire_all()
    kinds = {
        item.request_type
        for item in db.query(TokenUsage).filter(TokenUsage.user_id == caller.id).all()
    }
    assert "studio" in kinds


@pytest.mark.asyncio
async def test_stream_usage_bucket_follows_the_job_token_not_the_header(
    db: Session, monkeypatch
):
    captured: list[dict] = []

    def fake_stream(**kwargs):
        captured.append(kwargs)

        async def _empty():
            if False:
                yield ""

        return _empty()

    monkeypatch.setattr(proxy_api, "proxy_stream", fake_stream)
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)
    caller = make_user(db, username="studio-stream-caller", role="admin")
    model = make_model(db, name="studio-stream-model")

    header_only = _Request({
        "model": model.name,
        "stream": True,
        "messages": [{"role": "user", "content": "簡報"}],
    })
    header_only.headers = {"X-ANILA-Request-Source": "studio"}
    await proxy_api.chat_completions(
        header_only, caller=Caller(user=caller, api_key_id=None), db=db
    )
    assert captured[-1]["request_type"] == "chat"

    from app.services.studio_job_token import issue_studio_job_token

    token = issue_studio_job_token(
        user_id=caller.id,
        job_id="deck-stream",
        classification="無機密",
        token_version=int(caller.token_version or 0),
        db=db,
    )
    job = _Request({
        "model": model.name,
        "stream": True,
        "messages": [{"role": "user", "content": "簡報"}],
    })
    job.headers = {"Authorization": f"Bearer {token}"}
    await proxy_api.chat_completions(
        job, caller=Caller(user=caller, api_key_id=None), db=db
    )
    assert captured[-1]["request_type"] == "studio"
