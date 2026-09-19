"""拇指旁細分分數(rating_score):儲存、配對拒絕、舊列相容、治理讀取。"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import csv
import io
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.api.admin.feedback import FEEDBACK_ITEM_KEYS
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.auth_service import create_tokens
from tests.conftest import make_user

FEEDBACK_URL = "/api/admin/feedback"
SECRET_PAYLOAD = "CLASSIFIED-BODY-MUST-NOT-LEAK-VIA-SCORE-PATH"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _seed_assistant(db: Session, owner, *, content: str = "hi") -> tuple[Conversation, Message]:
    conv = Conversation(user_id=owner.id, title="score-seed")
    db.add(conv)
    db.flush()
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=content,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return conv, msg


def _rate(client, user, conv_id: int, message_id: int, body: dict):
    return client.put(
        f"/api/conversations/{conv_id}/messages/{message_id}/rating",
        headers=_bearer(user),
        json=body,
    )


def test_upper_range_score_is_stored_and_read_back(client, db: Session):
    """讚 + 8:寫入後 MessageOut 與治理列表都讀得到同一個數字。"""
    user = make_user(db, username="score-up-u", role="user")
    admin = make_user(db, username="score-up-admin", role="admin")
    conv, msg = _seed_assistant(db, user)

    resp = _rate(client, user, conv.id, msg.id, {"rating": "up", "rating_score": 8})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rating"] == "up"
    assert resp.json()["rating_score"] == 8

    db.refresh(msg)
    assert msg.rating == "up"
    assert msg.rating_score == 8

    admin_resp = client.get(FEEDBACK_URL, headers=_bearer(admin), params={"rating": "up"})
    assert admin_resp.status_code == 200
    item = next(i for i in admin_resp.json()["items"] if i["message_id"] == msg.id)
    assert item["rating"] == "up"
    assert item["rating_score"] == 8


def test_lower_range_score_is_stored_and_read_back(client, db: Session):
    """爛 + 2:寫入後 MessageOut 與治理列表都讀得到同一個數字。"""
    user = make_user(db, username="score-dn-u", role="user")
    admin = make_user(db, username="score-dn-admin", role="admin")
    conv, msg = _seed_assistant(db, user)

    resp = _rate(client, user, conv.id, msg.id, {"rating": "down", "rating_score": 2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rating"] == "down"
    assert resp.json()["rating_score"] == 2

    db.refresh(msg)
    assert msg.rating == "down"
    assert msg.rating_score == 2

    admin_resp = client.get(FEEDBACK_URL, headers=_bearer(admin), params={"rating": "down"})
    assert admin_resp.status_code == 200
    item = next(i for i in admin_resp.json()["items"] if i["message_id"] == msg.id)
    assert item["rating"] == "down"
    assert item["rating_score"] == 2


def test_score_that_contradicts_thumb_is_refused(client, db: Session):
    """配對是設計核心:讚不能帶 1–5,爛不能帶 6–10。"""
    user = make_user(db, username="score-bad-u", role="user")
    conv, msg = _seed_assistant(db, user)

    up_with_low = _rate(
        client, user, conv.id, msg.id, {"rating": "up", "rating_score": 3}
    )
    assert up_with_low.status_code == 400, up_with_low.text
    assert "6–10" in up_with_low.json()["detail"]

    down_with_high = _rate(
        client, user, conv.id, msg.id, {"rating": "down", "rating_score": 9}
    )
    assert down_with_high.status_code == 400, down_with_high.text
    assert "1–5" in down_with_high.json()["detail"]

    db.refresh(msg)
    assert msg.rating is None
    assert msg.rating_score is None


def test_legacy_thumb_only_rows_appear_on_governance_and_csv(client, db: Session):
    """既有只有 up／down、沒有數字的列仍要正確出現在治理頁與匯出。"""
    admin = make_user(db, username="score-legacy-admin", role="admin")
    owner = make_user(db, username="score-legacy-u", role="user")
    conv = Conversation(user_id=owner.id, title="legacy-thumb")
    db.add(conv)
    db.flush()
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content="old reply",
        rating="down",
        rating_score=None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)

    json_resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"rating": "down"}
    )
    assert json_resp.status_code == 200
    item = next(i for i in json_resp.json()["items"] if i["message_id"] == msg.id)
    assert item["rating"] == "down"
    assert item["rating_score"] is None
    assert set(item.keys()) == FEEDBACK_ITEM_KEYS

    csv_resp = client.get(
        FEEDBACK_URL,
        headers=_bearer(admin),
        params={"rating": "down", "format": "csv"},
    )
    assert csv_resp.status_code == 200
    text = csv_resp.text
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text[1:])))
    assert rows[0][0] == "評分"
    assert rows[0][1] == "分數（好評 6–10／差評 1–5）"
    # 9ef47d7a 之後 CSV 在訊息 ID 後面多了提問／回覆兩欄;舊列仍要進檔。
    message_id_idx = rows[0].index("訊息 ID")
    data = [r for r in rows[1:] if r[message_id_idx] == str(msg.id)]
    assert data, "舊列沒進 CSV"
    assert data[0][0] == "差評"
    assert data[0][1] == ""
    assert rows[0][-2:] == ["使用者提問", "被評分回覆"]
    assert data[0][message_id_idx + 2] == "old reply"


def test_csv_still_cannot_carry_message_content_with_score(client, db: Session):
    """細分分數上線後,JSON 列表仍不得夾帶訊息正文;CSV 只在明示欄位帶被評分回覆。

    9ef47d7a 把 CSV 從「同一份 JSON 白名單」改成另外附提問／回覆,並對密等
    >= 營業秘密落 access_classified_conversation。本條釘的是:JSON 沒有正文、
    沒有 content 欄、CSV 只在「被評分回覆」出現密文,且列管匯出有稽核。
    """
    from app.models.audit_log import AuditLog

    admin = make_user(db, username="score-csv-admin", role="admin")
    owner = make_user(db, username="score-csv-u", role="user")
    conv = Conversation(
        user_id=owner.id, title="secret-score", classification_level="機密"
    )
    db.add(conv)
    db.flush()
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=SECRET_PAYLOAD,
        rating="up",
        rating_score=9,
        classification_level="機密",
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()

    json_resp = client.get(FEEDBACK_URL, headers=_bearer(admin))
    assert json_resp.status_code == 200, json_resp.text
    assert SECRET_PAYLOAD not in json_resp.text
    item = next(i for i in json_resp.json()["items"] if i["message_id"] == msg.id)
    assert "content" not in item
    assert set(item.keys()) == FEEDBACK_ITEM_KEYS

    resp = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"format": "csv"}
    )
    assert resp.status_code == 200, resp.text
    rows = list(csv.reader(io.StringIO(resp.text.lstrip("﻿"))))
    assert "content" not in rows[0]
    assert rows[0][-2:] == ["使用者提問", "被評分回覆"]
    assert len(rows[0]) == len(FEEDBACK_ITEM_KEYS) + 2
    scored = [r for r in rows[1:] if r[rows[0].index("訊息 ID")] == str(msg.id)]
    assert scored
    assert scored[0][-1] == SECRET_PAYLOAD
    assert scored[0][1] == "9"
    assert resp.text.count(SECRET_PAYLOAD) == 1

    audits = (
        db.query(AuditLog)
        .filter(AuditLog.action == "access_classified_conversation")
        .all()
    )
    assert len(audits) == 1
    assert audits[0].actor_user_id == admin.id


def test_thumb_without_score_stays_null(client, db: Session):
    """只按拇指不選數字 → rating_score 維持 NULL(低摩擦路徑)。"""
    user = make_user(db, username="score-thumb-only", role="user")
    conv, msg = _seed_assistant(db, user)

    resp = _rate(client, user, conv.id, msg.id, {"rating": "up"})
    assert resp.status_code == 200
    assert resp.json()["rating"] == "up"
    assert resp.json()["rating_score"] is None
