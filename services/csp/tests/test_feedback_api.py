"""P3.4 使用者回饋總覽 —— 授權、篩選、密等不洩訊息正文。"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import csv
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.api.admin import feedback as feedback_api
from app.api.admin.feedback import FEEDBACK_ITEM_KEYS, FeedbackItem
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.auth_service import create_tokens
from app.utils.csv_formula import FORMULA_TRIGGER_PREFIXES
from tests.conftest import make_user

FEEDBACK_URL = "/api/admin/feedback"

SECRET_PAYLOAD = "CLASSIFIED-BODY-SHOULD-NEVER-APPEAR-IN-FEEDBACK-LIST"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _seed_rated_message(
    db: Session,
    *,
    owner,
    rating: str = "down",
    rating_score: int | None = None,
    content: str = "assistant reply",
    classification_level: str = "無機密",
    model_name: str = "gpt-oss",
    agent_name: str = "briefing-agent",
    comment: str | None = "答非所問",
    reasons: list | None = None,
) -> Message:
    conv = Conversation(
        user_id=owner.id,
        title="feedback-seed",
        classification_level=classification_level,
    )
    db.add(conv)
    db.flush()
    meta = None
    if comment is not None or reasons:
        meta = {
            "feedback": {
                "comment": comment,
                "reasons": reasons or [],
            }
        }
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=content,
        rating=rating,
        rating_score=rating_score,
        model_name=model_name,
        agent_name=agent_name,
        classification_level=classification_level,
        metadata_=meta,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _rows_from_csv_text(text: str) -> list[list[str]]:
    """Decode a CSV body into rows, BOM stripped.

    Goes through ``csv.reader`` rather than ``splitlines`` on purpose — a
    留言 may contain a newline, and counting lines would then over-report.
    """
    assert text.startswith("﻿"), "少了 BOM,Excel 會把繁體中文開成亂碼"
    return list(csv.reader(io.StringIO(text[1:])))


def _csv_rows(resp) -> list[list[str]]:
    return _rows_from_csv_text(resp.text)


def test_feedback_requires_authentication(client):
    assert client.get(FEEDBACK_URL).status_code == 401


@pytest.mark.parametrize("role", ["user", "developer"])
def test_feedback_rejects_non_admin(client, db: Session, role: str):
    user = make_user(db, username=f"fb-{role}", role=role)
    assert client.get(FEEDBACK_URL, headers=_bearer(user)).status_code == 403


def test_feedback_admin_sees_rating_and_comment(client, db: Session):
    admin = make_user(db, username="fb-admin", role="admin")
    owner = make_user(db, username="fb-owner-user", role="user")
    _seed_rated_message(db, owner=owner, rating="down", comment="亂講")

    resp = client.get(FEEDBACK_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["down"] >= 1
    assert body["items"]
    item = body["items"][0]
    assert item["rating"] == "down"
    assert item["comment"] == "亂講"
    assert item["agent_name"] == "briefing-agent"
    assert item["model_name"] == "gpt-oss"


def test_feedback_owner_also_allowed(client, db: Session):
    owner = make_user(db, username="fb-platform-owner", role="owner")
    user = make_user(db, username="fb-u2", role="user")
    _seed_rated_message(db, owner=user, rating="up", comment=None)

    assert client.get(FEEDBACK_URL, headers=_bearer(owner)).status_code == 200


def test_feedback_filter_by_rating_and_agent(client, db: Session):
    admin = make_user(db, username="fb-filter-admin", role="admin")
    owner = make_user(db, username="fb-filter-u", role="user")
    _seed_rated_message(
        db, owner=owner, rating="down", agent_name="agent-a", comment="a"
    )
    _seed_rated_message(
        db, owner=owner, rating="up", agent_name="agent-b", comment=None
    )

    resp = client.get(
        FEEDBACK_URL,
        headers=_bearer(admin),
        params={"rating": "down", "agent_name": "agent-a"},
    )

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items
    assert all(i["rating"] == "down" and i["agent_name"] == "agent-a" for i in items)


def test_feedback_never_returns_message_content_even_for_secret(
    client, db: Session
):
    """Classification rule: list must not become a bulk read of controlled text.

    Existing conversation GET lets admin-tier read bodies (with audit ≥營業秘密).
    This list endpoint must never include ``content``, including when the row
    is 機密 and the caller is admin — otherwise the governance console becomes
    a side channel past the audited read path.
    """
    admin = make_user(db, username="fb-secret-admin", role="admin")
    owner = make_user(db, username="fb-secret-u", role="user")
    _seed_rated_message(
        db,
        owner=owner,
        rating="down",
        content=SECRET_PAYLOAD,
        classification_level="機密",
        comment="仍看得到留言",
    )

    resp = client.get(FEEDBACK_URL, headers=_bearer(admin))

    assert resp.status_code == 200, resp.text
    raw = resp.text
    assert SECRET_PAYLOAD not in raw
    body = resp.json()
    assert body["items"]
    item = body["items"][0]
    assert "content" not in item
    assert set(item.keys()) == FEEDBACK_ITEM_KEYS
    assert item["classification_level"] == "機密"
    assert item["comment"] == "仍看得到留言"


def test_feedback_response_fields_are_exactly_the_whitelist(client, db: Session):
    admin = make_user(db, username="fb-wl-admin", role="admin")
    owner = make_user(db, username="fb-wl-u", role="user")
    _seed_rated_message(db, owner=owner)

    body = client.get(FEEDBACK_URL, headers=_bearer(admin)).json()

    assert set(body.keys()) == {"summary", "items"}
    assert set(body["summary"].keys()) == {"total", "up", "down", "with_comment"}
    for item in body["items"]:
        assert set(item.keys()) == FEEDBACK_ITEM_KEYS
        assert "content" not in item


# ── CSV 匯出 ────────────────────────────────────────────────────────────────


def test_feedback_csv_export_is_really_csv(client, db: Session):
    """Content-Type 必須是 text/csv。

    SPA catch-all 會用 ``200 text/html`` 冒充「端點活著」,所以這裡釘的是
    型別與 Content-Disposition,不是 status code。
    """
    admin = make_user(db, username="fb-csv-admin", role="admin")
    owner = make_user(db, username="fb-csv-u", role="user")
    _seed_rated_message(db, owner=owner, rating="down", comment="太慢了")

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=feedback-")
    assert disposition.endswith(".csv")

    rows = _csv_rows(resp)
    assert rows[0][:4] == ["評分", "分數(讚6-10／爛1-5)", "留言", "原因"]
    assert rows[1][0] == "爛"
    assert rows[1][1] == ""  # 舊列沒有細分分數
    assert rows[1][2] == "太慢了"


def test_feedback_csv_never_contains_message_content(client, db: Session):
    """匯出不得成為受控對話正文的批量外流路徑。

    這條同時盯兩個走位:(a) 正文被塞進任何一欄;(b) 有人放寬白名單,
    讓 CSV 長出 JSON 擋掉的欄位。
    """
    admin = make_user(db, username="fb-csv-secret-admin", role="admin")
    owner = make_user(db, username="fb-csv-secret-u", role="user")
    _seed_rated_message(
        db,
        owner=owner,
        rating="down",
        content=SECRET_PAYLOAD,
        classification_level="機密",
        comment="留言看得到,正文看不到",
    )

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )

    assert resp.status_code == 200, resp.text
    assert SECRET_PAYLOAD not in resp.text

    rows = _csv_rows(resp)
    assert "content" not in rows[0], "白名單被放寬,CSV 長出了正文欄"
    assert len(rows[0]) == len(FEEDBACK_ITEM_KEYS)
    assert rows[1][6] == "機密"
    assert rows[1][2] == "留言看得到,正文看不到"


def test_feedback_csv_honours_the_filters(client, db: Session):
    """匯出的必須就是畫面上那組篩選的結果,不是整庫倒出來。"""
    admin = make_user(db, username="fb-csv-filter-admin", role="admin")
    owner = make_user(db, username="fb-csv-filter-u", role="user")
    _seed_rated_message(db, owner=owner, rating="down", agent_name="agent-a")
    _seed_rated_message(db, owner=owner, rating="down", agent_name="agent-a")
    _seed_rated_message(db, owner=owner, rating="up", agent_name="agent-b")

    params = {"rating": "down", "agent_name": "agent-a"}
    json_items = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params=params
    ).json()["items"]
    csv_resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={**params, "format": "csv"}
    )

    data_rows = _csv_rows(csv_resp)[1:]
    assert len(json_items) == 2
    assert len(data_rows) == len(json_items)
    assert all(row[4] == "agent-a" and row[0] == "爛" for row in data_rows)
    # 被篩掉的那列真的不在檔案裡
    assert "agent-b" not in csv_resp.text


def test_feedback_csv_exports_whole_filtered_set_not_just_the_page(
    client, db: Session
):
    """``limit`` 是畫面分頁,不該讓匯出只給一頁。"""
    admin = make_user(db, username="fb-csv-page-admin", role="admin")
    owner = make_user(db, username="fb-csv-page-u", role="user")
    for _ in range(3):
        _seed_rated_message(db, owner=owner, rating="down")

    resp = client.get(
        FEEDBACK_URL,
        headers=_bearer(admin),
        params={"format": "csv", "limit": 1},
    )

    assert len(_csv_rows(resp)[1:]) == 3


def test_feedback_csv_ceiling_tells_the_caller_instead_of_truncating(
    client, db: Session, monkeypatch
):
    """超過上限要**明講**。默默給一份短檔案是這個專案在抓的缺陷類型。"""
    monkeypatch.setattr(feedback_api, "FEEDBACK_EXPORT_MAX_ROWS", 2)
    admin = make_user(db, username="fb-csv-cap-admin", role="admin")
    owner = make_user(db, username="fb-csv-cap-u", role="user")
    for _ in range(3):
        _seed_rated_message(db, owner=owner, rating="down")

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )

    assert resp.status_code == 400, resp.text
    assert "text/csv" not in resp.headers["content-type"]
    detail = resp.json()["detail"]
    assert "上限 2 列" in detail
    assert "沒有匯出" in detail
    assert SECRET_PAYLOAD not in resp.text


def test_feedback_csv_ceiling_not_hit_exports_everything(
    client, db: Session, monkeypatch
):
    """上限沒踩到就不該礙事 —— 剛好等於上限仍要正常匯出。"""
    monkeypatch.setattr(feedback_api, "FEEDBACK_EXPORT_MAX_ROWS", 2)
    admin = make_user(db, username="fb-csv-cap2-admin", role="admin")
    owner = make_user(db, username="fb-csv-cap2-u", role="user")
    for _ in range(2):
        _seed_rated_message(db, owner=owner, rating="down")

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )

    assert resp.status_code == 200, resp.text
    assert len(_csv_rows(resp)[1:]) == 2


def test_feedback_csv_requires_authentication(client):
    assert client.get(FEEDBACK_URL, params={"format": "csv"}).status_code == 401


@pytest.mark.parametrize("role", ["user", "developer"])
def test_feedback_csv_rejects_non_admin(client, db: Session, role: str):
    """匯出面的授權必須跟 JSON 面一模一樣,不能有旁路。"""
    user = make_user(db, username=f"fb-csv-{role}", role=role)
    headers = _bearer(user)

    json_resp = client.get(FEEDBACK_URL, headers=headers)
    csv_resp = client.get(FEEDBACK_URL, headers=headers, params={"format": "csv"})

    assert json_resp.status_code == 403
    assert csv_resp.status_code == json_resp.status_code
    assert "text/csv" not in csv_resp.headers["content-type"]


# ── CSV formula injection (site 2) ─────────────────────────────────────────


_COMMENT_COL = 2
_REASONS_COL = 3
_HTTP_COMMENT_TRIGGERS = ("=", "+", "-", "@")


def _comment_item(payload: str) -> FeedbackItem:
    return FeedbackItem(
        message_id=1,
        conversation_id=1,
        rating="down",
        comment=payload,
        reasons=[],
        classification_level="無機密",
        message_created_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize("prefix", FORMULA_TRIGGER_PREFIXES)
def test_items_to_csv_neutralizes_comment_for_each_trigger(prefix: str):
    """Producer-level lock. Ingest ``.strip()`` eats leading ``\\t``/``\\r``
    before export, so the six-char set is asserted here on ``_items_to_csv``.
    """
    payload = f"{prefix}1+1"
    text = feedback_api._items_to_csv([_comment_item(payload)])
    cell = _rows_from_csv_text(text)[1][_COMMENT_COL]
    assert cell[:1] == "'"
    assert cell[1:] == payload


@pytest.mark.parametrize("prefix", _HTTP_COMMENT_TRIGGERS)
def test_feedback_csv_neutralizes_comment_formula_prefix(
    client, db: Session, prefix: str
):
    """HTTP path for the triggers that survive metadata ``strip()``."""
    tag = prefix.encode("unicode_escape").decode("ascii")
    admin = make_user(db, username=f"fb-inj-c-admin-{tag}", role="admin")
    owner = make_user(db, username=f"fb-inj-c-u-{tag}", role="user")
    payload = f"{prefix}1+1"
    _seed_rated_message(db, owner=owner, comment=payload, reasons=None)

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )
    assert resp.status_code == 200, resp.text
    cell = _csv_rows(resp)[1][_COMMENT_COL]
    assert cell[:1] == "'"
    assert cell[1:] == payload


@pytest.mark.parametrize("prefix", FORMULA_TRIGGER_PREFIXES)
def test_feedback_csv_neutralizes_reasons_first_element(
    client, db: Session, prefix: str
):
    """``reasons[0]`` is a separate branch from ``comment`` (join, not str()).

    The original finding named only ``comment``. After join, only the first
    element's first character sits at the start of the cell.
    """
    tag = prefix.encode("unicode_escape").decode("ascii")
    admin = make_user(db, username=f"fb-inj-r-admin-{tag}", role="admin")
    owner = make_user(db, username=f"fb-inj-r-u-{tag}", role="user")
    first = f"{prefix}HYPERLINK(\"http://x\")"
    _seed_rated_message(
        db,
        owner=owner,
        comment="safe comment",
        reasons=[first, "not-at-cell-start"],
    )

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )
    assert resp.status_code == 200, resp.text
    rows = _csv_rows(resp)
    comment_cell = rows[1][_COMMENT_COL]
    reasons_cell = rows[1][_REASONS_COL]
    assert comment_cell == "safe comment"
    assert reasons_cell[:1] == "'"
    assert reasons_cell[1:].startswith(first)
    assert "not-at-cell-start" in reasons_cell
