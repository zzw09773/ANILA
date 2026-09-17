"""Reasoning overflow must not 413 a legitimate assistant persist.

Astra 2026-09-17：64KB 整包上限留下；只因 reasoning 超量時縮開頭、標
full/truncated/omitted，與生成狀態分開；其他欄位超限仍 413。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.message_metadata import (
    MAX_METADATA_BYTES,
    REASONING_PERSIST_KEY,
    fit_reasoning_into_metadata_budget,
    metadata_utf8_size,
    prepare_message_metadata,
)
from tests.conftest import login, make_user
from tests.test_message_reserve_reply import _auth, _create_conv, _turn


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _meta_bytes(meta: dict) -> int:
    return len(json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def test_fit_keeps_short_reasoning_as_full():
    out = fit_reasoning_into_metadata_budget({"reasoning": "想了一下", "usage": {"reasoning_tokens": 12}})
    persist = out[REASONING_PERSIST_KEY]
    assert persist["status"] == "full"
    assert persist["original_chars"] == persist["kept_chars"] == 4
    assert "reason" not in persist
    assert out["reasoning"] == "想了一下"
    assert out["usage"]["reasoning_tokens"] == 12
    assert metadata_utf8_size(out) <= MAX_METADATA_BYTES


def test_fit_truncates_from_the_start_and_counts_json_escapes():
    # Quotes / backslashes expand in JSON; CJK + emoji must stay valid Unicode.
    head = '開頭「計畫」\\"quote\\" 🧠'
    tail = "尾段結論不該被當成答案保存"
    reasoning = head + ("中段。" * 8000) + tail
    out = fit_reasoning_into_metadata_budget({"reasoning": reasoning, "trace": []})
    persist = out[REASONING_PERSIST_KEY]
    assert persist["status"] == "truncated"
    assert persist["reason"] == "over_budget"
    assert persist["original_chars"] == len(reasoning)
    assert 0 < persist["kept_chars"] < persist["original_chars"]
    kept = out["reasoning"]
    assert kept == reasoning[: persist["kept_chars"]]
    assert kept.startswith(head)
    assert tail not in kept
    assert metadata_utf8_size(out) <= MAX_METADATA_BYTES
    # 再量一次：截斷標記＋跳脫都已算進去，不能靠字數矇混。
    assert _meta_bytes(out) <= MAX_METADATA_BYTES


def test_fit_omits_when_other_fields_leave_no_meaningful_fragment():
    pad = "P" * (MAX_METADATA_BYTES - 180)
    reasoning = "這段思考很長" * 200
    out = fit_reasoning_into_metadata_budget({"reasoning": reasoning, "pad": pad})
    persist = out[REASONING_PERSIST_KEY]
    assert persist["status"] == "omitted"
    assert persist["reason"] == "over_budget"
    assert persist["kept_chars"] == 0
    assert persist["original_chars"] == len(reasoning)
    assert "reasoning" not in out
    assert metadata_utf8_size(out) <= MAX_METADATA_BYTES


def test_prepare_still_413s_when_other_metadata_alone_is_over_budget():
    pad = "X" * (MAX_METADATA_BYTES + 64)
    with pytest.raises(HTTPException) as exc:
        prepare_message_metadata({"pad": pad, "reasoning": "短"})
    assert exc.value.status_code == 413
    assert exc.value.detail == "metadata 過大"


def test_put_long_reasoning_keeps_body_and_persist_status(client, db):
    _user, headers = _auth(client, db, username="reason_budget_body")
    cid = _create_conv(client, headers)["id"]
    writer = "w-reason-body-token"
    mid = _turn(client, headers, cid, "問題", writer=writer).json()["assistant"]["id"]
    reasoning = "思考開頭。" + ("長。" * 20000)
    put = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={
            "content": "這是完整正文。",
            "stream_writer": writer,
            "metadata": {
                "anila_stream": {"state": "complete"},
                "reasoning": reasoning,
                "usage": {"reasoning_tokens": 8888},
                "classified": False,
            },
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    persist = put.json()["metadata"][REASONING_PERSIST_KEY]
    assert persist["status"] == "truncated"
    assert persist["reason"] == "over_budget"
    assert put.json()["content"] == "這是完整正文。"
    assert put.json()["metadata"]["usage"]["reasoning_tokens"] == 8888
    assert put.json()["metadata"]["anila_stream"]["state"] == "complete"
    assert put.json()["metadata"]["reasoning"] == reasoning[: persist["kept_chars"]]

    reload_1 = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = next(m for m in reload_1["messages"] if m["id"] == mid)
    assert row["content"] == "這是完整正文。"
    assert row["metadata"][REASONING_PERSIST_KEY] == persist
    assert row["metadata"]["reasoning"] == put.json()["metadata"]["reasoning"]
    assert row["metadata"]["usage"]["reasoning_tokens"] == 8888

    reload_2 = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row2 = next(m for m in reload_2["messages"] if m["id"] == mid)
    assert row2["content"] == row["content"]
    assert row2["metadata"][REASONING_PERSIST_KEY]["status"] == "truncated"


def test_put_empty_length_reply_is_not_a_successful_answer(client, db):
    _user, headers = _auth(client, db, username="reason_budget_empty")
    cid = _create_conv(client, headers)["id"]
    writer = "w-reason-empty-token"
    mid = _turn(client, headers, cid, "深入一題", writer=writer).json()["assistant"]["id"]
    reasoning = "空正文前的思考。" + ("想。" * 20000)
    put = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={
            "content": "",
            "stream_writer": writer,
            "metadata": {
                "anila_stream": {"state": "complete"},
                "reasoning": reasoning,
                "usage": {"reasoning_tokens": 7777},
            },
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["content"] == ""
    assert body["metadata"]["anila_stream"]["state"] == "complete"
    assert body["metadata"][REASONING_PERSIST_KEY]["status"] == "truncated"
    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = next(m for m in stored["messages"] if m["id"] == mid)
    assert row["content"] == ""
    assert row["metadata"][REASONING_PERSIST_KEY]["status"] == "truncated"
    assert row["metadata"]["usage"]["reasoning_tokens"] == 7777


def test_put_other_metadata_over_budget_still_413s_and_keeps_row(client, db):
    _user, headers = _auth(client, db, username="reason_budget_413")
    cid = _create_conv(client, headers)["id"]
    writer = "w-reason-413-token"
    mid = _turn(client, headers, cid, "問題", writer=writer).json()["assistant"]["id"]
    put = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={
            "content": "這句不該因為 metadata 違規而落庫",
            "stream_writer": writer,
            "metadata": {
                "anila_stream": {"state": "complete"},
                "pad": "Z" * (MAX_METADATA_BYTES + 32),
                "reasoning": "短思考",
            },
        },
        headers=headers,
    )
    assert put.status_code == 413, put.text
    assert put.json()["detail"] == "metadata 過大"
    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = next(m for m in stored["messages"] if m["id"] == mid)
    assert row["content"] == ""
    assert row["metadata"]["anila_stream"]["state"] == "reserved"


def test_put_classified_uses_same_budget_not_a_shorter_one(client, db):
    _user, headers = _auth(client, db, username="reason_budget_secret")
    cid = _create_conv(client, headers)["id"]
    writer = "w-reason-secret-token"
    mid = _turn(client, headers, cid, "列管題", writer=writer).json()["assistant"]["id"]
    reasoning = "列管思考開頭。" + ("密。" * 18000)
    put = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={
            "content": "列管正文",
            "stream_writer": writer,
            "metadata": {
                "anila_stream": {"state": "complete"},
                "classified": True,
                "reasoning": reasoning,
            },
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    meta = put.json()["metadata"]
    assert meta["classified"] is True
    assert meta[REASONING_PERSIST_KEY]["status"] == "truncated"
    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = next(m for m in stored["messages"] if m["id"] == mid)
    assert row["content"] == "列管正文"
    assert row["metadata"]["classified"] is True
    assert row["metadata"][REASONING_PERSIST_KEY]["status"] == "truncated"
