"""差評結案、只通知回報者、已修好轉成回歸題。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.handoff import Notification
from app.models.message import Message
from app.services.auth_service import create_tokens
from app.services.feedback_eval import build_regression_case, case_to_jsonl
from tests.conftest import make_user

FEEDBACK_URL = "/api/admin/feedback"
QUESTION = "合成問句：圖書館週末開嗎？"
ANSWER = "合成回答：週末休館。"
THINK = "ANILA_RAW_MARKER_eval_7f3c"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _seed(
    db: Session,
    owner,
    *,
    rating: str = "down",
    content: str = ANSWER,
    user_content: str | None = QUESTION,
    classification_level: str = "無機密",
    status: str | None = None,
    resolution: str | None = None,
    note: str | None = None,
    submitter_id: int | None = None,
) -> Message:
    conv = Conversation(
        user_id=owner.id,
        title="feedback-closure",
        classification_level=classification_level,
    )
    db.add(conv)
    db.flush()
    parent_id = None
    if user_content is not None:
        user_msg = Message(
            conversation_id=conv.id,
            role="user",
            content=user_content,
            classification_level=classification_level,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user_msg)
        db.flush()
        parent_id = user_msg.id
    msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content=content,
        parent_id=parent_id,
        rating=rating,
        classification_level=classification_level,
        feedback_status=status,
        feedback_resolution=resolution,
        feedback_resolution_note=note,
        feedback_submitter_id=submitter_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _rate(client, user, msg: Message, body: dict):
    return client.put(
        f"/api/conversations/{msg.conversation_id}/messages/{msg.id}/rating",
        headers=_bearer(user),
        json=body,
    )


def _notices(db: Session) -> list[Notification]:
    db.expire_all()
    return db.query(Notification).filter(Notification.type == "feedback_resolved").all()


def test_down_vote_opens_a_case_and_up_vote_does_not(client, db: Session):
    user = make_user(db, username="fb-case-u", role="user")
    down = _seed(db, user, rating=None, user_content=QUESTION)
    up = _seed(db, user, rating=None, user_content=QUESTION)

    down_resp = _rate(client, user, down, {"rating": "down", "comment": "答非所問"})
    up_resp = _rate(client, user, up, {"rating": "up"})
    assert down_resp.status_code == 200, down_resp.text
    assert up_resp.status_code == 200, up_resp.text

    db.expire_all()
    db.refresh(down)
    db.refresh(up)
    assert down.feedback_status == "open"
    assert down.feedback_submitter_id == user.id
    assert down.feedback_resolution is None
    assert up.feedback_status is None
    assert up.feedback_submitter_id is None


def test_close_fixed_notifies_only_the_submitter(client, db: Session):
    owner = make_user(db, username="fb-close-owner", role="user")
    admin = make_user(db, username="fb-close-admin", role="admin")
    other = make_user(db, username="fb-close-other", role="user")
    msg = _seed(db, owner, rating=None)
    assert _rate(client, owner, msg, {"rating": "down", "comment": "不對"}).status_code == 200

    resp = client.post(
        f"{FEEDBACK_URL}/{msg.id}/close",
        headers=_bearer(admin),
        json={"resolution": "fixed"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["notified"] is True
    assert body["unchanged"] is False
    assert body["item"]["feedback_status"] == "closed"
    assert body["item"]["feedback_resolution"] == "fixed"
    assert ANSWER not in resp.text
    assert "content" not in body["item"]

    notices = _notices(db)
    assert len(notices) == 1
    assert notices[0].user_id == owner.id
    assert notices[0].title == "你回報的問題已修好"
    assert QUESTION not in notices[0].body
    assert ANSWER not in notices[0].body
    assert QUESTION not in json.dumps(notices[0].payload, ensure_ascii=False)

    owner_rows = client.get("/api/notifications", headers=_bearer(owner)).json()
    admin_rows = client.get("/api/notifications", headers=_bearer(admin)).json()
    other_rows = client.get("/api/notifications", headers=_bearer(other)).json()
    assert [row["type"] for row in owner_rows] == ["feedback_resolved"]
    assert admin_rows == []
    assert other_rows == []


def test_admin_who_rated_is_notified_instead_of_the_owner(client, db: Session):
    owner = make_user(db, username="fb-admin-rate-owner", role="user")
    admin = make_user(db, username="fb-admin-rate-admin", role="admin")
    msg = _seed(db, owner, rating=None)
    assert _rate(client, admin, msg, {"rating": "down"}).status_code == 200

    resp = client.post(
        f"{FEEDBACK_URL}/{msg.id}/close",
        headers=_bearer(admin),
        json={"resolution": "duplicate"},
    )
    assert resp.status_code == 200, resp.text
    notices = _notices(db)
    assert len(notices) == 1
    assert notices[0].user_id == admin.id
    assert notices[0].title == "你回報的問題與既有項目重複"


def test_legacy_row_without_submitter_notifies_the_owner(client, db: Session):
    owner = make_user(db, username="fb-legacy-owner", role="user")
    admin = make_user(db, username="fb-legacy-admin", role="admin")
    msg = _seed(db, owner, rating="down")

    resp = client.post(
        f"{FEEDBACK_URL}/{msg.id}/close",
        headers=_bearer(admin),
        json={"resolution": "fixed"},
    )
    assert resp.status_code == 200, resp.text
    assert _notices(db)[0].user_id == owner.id


def test_wont_fix_requires_a_reason_and_duplicate_does_not(client, db: Session):
    owner = make_user(db, username="fb-reason-owner", role="user")
    admin = make_user(db, username="fb-reason-admin", role="admin")
    missing = _seed(db, owner)
    blank = _seed(db, owner)
    duplicate = _seed(db, owner)

    no_note = client.post(
        f"{FEEDBACK_URL}/{missing.id}/close",
        headers=_bearer(admin),
        json={"resolution": "wont_fix"},
    )
    spaces = client.post(
        f"{FEEDBACK_URL}/{blank.id}/close",
        headers=_bearer(admin),
        json={"resolution": "wont_fix", "note": "   "},
    )
    ok = client.post(
        f"{FEEDBACK_URL}/{duplicate.id}/close",
        headers=_bearer(admin),
        json={"resolution": "duplicate"},
    )
    assert no_note.status_code == 400
    assert "原因" in no_note.json()["detail"]
    assert spaces.status_code == 400
    assert ok.status_code == 200, ok.text
    assert ok.json()["item"]["feedback_resolution"] == "duplicate"

    told = client.post(
        f"{FEEDBACK_URL}/{missing.id}/close",
        headers=_bearer(admin),
        json={"resolution": "wont_fix", "note": "題目超出目前能查的範圍"},
    )
    assert told.status_code == 200, told.text
    notice = next(row for row in _notices(db) if row.user_id == owner.id and "原因：" in row.body)
    assert "題目超出目前能查的範圍" in notice.body
    assert notice.title == "你回報的問題這次不修"


def test_repeating_the_same_close_does_not_notify_again(client, db: Session):
    owner = make_user(db, username="fb-repeat-owner", role="user")
    admin = make_user(db, username="fb-repeat-admin", role="admin")
    msg = _seed(db, owner)
    payload = {"resolution": "wont_fix", "note": "維持現況"}
    first = client.post(f"{FEEDBACK_URL}/{msg.id}/close", headers=_bearer(admin), json=payload)
    second = client.post(f"{FEEDBACK_URL}/{msg.id}/close", headers=_bearer(admin), json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["unchanged"] is True
    assert second.json()["notified"] is False
    assert len(_notices(db)) == 1

    changed = client.post(
        f"{FEEDBACK_URL}/{msg.id}/close",
        headers=_bearer(admin),
        json={"resolution": "fixed"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["notified"] is True
    assert len(_notices(db)) == 2


def test_new_down_vote_reopens_a_closed_case(client, db: Session):
    owner = make_user(db, username="fb-reopen-owner", role="user")
    admin = make_user(db, username="fb-reopen-admin", role="admin")
    msg = _seed(db, owner, rating=None)
    assert _rate(client, owner, msg, {"rating": "down", "comment": "第一次"}).status_code == 200
    assert client.post(
        f"{FEEDBACK_URL}/{msg.id}/close",
        headers=_bearer(admin),
        json={"resolution": "fixed"},
    ).status_code == 200

    same = _rate(client, owner, msg, {"rating": "down"})
    assert same.status_code == 200
    db.expire_all()
    db.refresh(msg)
    assert msg.feedback_status == "closed"

    updated = _rate(client, owner, msg, {"rating": "down", "comment": "還是錯"})
    assert updated.status_code == 200
    db.expire_all()
    db.refresh(msg)
    assert msg.feedback_status == "open"
    assert msg.feedback_resolution is None
    assert msg.feedback_resolved_at is None


def test_clearing_the_rating_drops_the_case(client, db: Session):
    owner = make_user(db, username="fb-clear-owner", role="user")
    msg = _seed(db, owner, rating=None)
    assert _rate(client, owner, msg, {"rating": "down"}).status_code == 200
    assert _rate(client, owner, msg, {"rating": None}).status_code == 200
    db.expire_all()
    db.refresh(msg)
    assert msg.rating is None
    assert msg.feedback_status is None
    assert msg.feedback_submitter_id is None


def test_status_filter_and_non_steward_cannot_close(client, db: Session):
    admin = make_user(db, username="fb-filter-close-admin", role="admin")
    owner = make_user(db, username="fb-filter-close-owner", role="user")
    developer = make_user(db, username="fb-filter-close-dev", role="developer")
    open_msg = _seed(db, owner, status="open", submitter_id=owner.id)
    closed_msg = _seed(
        db, owner, status="closed", resolution="fixed", submitter_id=owner.id,
    )
    legacy = _seed(db, owner)

    denied = client.post(
        f"{FEEDBACK_URL}/{open_msg.id}/close",
        headers=_bearer(developer),
        json={"resolution": "fixed"},
    )
    assert denied.status_code == 403

    open_rows = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"status": "open"},
    ).json()["items"]
    closed_rows = client.get(
        FEEDBACK_URL, headers=_bearer(admin), params={"status": "closed"},
    ).json()["items"]
    open_ids = {row["message_id"] for row in open_rows}
    closed_ids = {row["message_id"] for row in closed_rows}
    assert open_msg.id in open_ids
    assert legacy.id in open_ids
    assert closed_msg.id not in open_ids
    assert closed_ids == {closed_msg.id}


def test_eval_case_is_the_original_question_only(client, db: Session):
    owner = make_user(db, username="fb-eval-owner", role="user")
    admin = make_user(db, username="fb-eval-admin", role="admin")
    deputy = make_user(db, username="fb-eval-deputy", role="deputy")
    msg = _seed(
        db,
        owner,
        user_content=f"<think>{THINK}</think>{QUESTION}",
        content=f"<think>{THINK}</think>{ANSWER}",
        status="closed",
        resolution="fixed",
        submitter_id=owner.id,
    )
    open_msg = _seed(db, owner, status="open", submitter_id=owner.id)

    blocked = client.post(f"{FEEDBACK_URL}/{open_msg.id}/eval-case", headers=_bearer(admin))
    assert blocked.status_code == 409
    assert "已修好" in blocked.json()["detail"]

    deputy_resp = client.post(f"{FEEDBACK_URL}/{msg.id}/eval-case", headers=_bearer(deputy))
    assert deputy_resp.status_code == 403

    resp = client.post(f"{FEEDBACK_URL}/{msg.id}/eval-case", headers=_bearer(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == f"fb-{msg.id}"
    assert body["prompt"] == QUESTION
    assert ANSWER not in resp.text
    assert THINK not in resp.text
    assert body["category"] == "feedback-regression"
    loaded = json.loads(body["jsonl"])
    assert loaded["prompt"] == QUESTION
    assert loaded["system_mode"] == "preamble"

    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo / "tools" / "eval"))
    import run_golden_set

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as fh:
        fh.write(body["jsonl"] + "\n")
        path = Path(fh.name)
    try:
        cases = run_golden_set._load_cases(path)
    finally:
        path.unlink(missing_ok=True)
    assert cases[0]["id"] == body["id"]
    assert run_golden_set._validate_only(cases) == 0


def test_eval_case_audits_classified_conversations(client, db: Session):
    owner = make_user(db, username="fb-eval-secret-owner", role="user")
    admin = make_user(db, username="fb-eval-secret-admin", role="admin")
    msg = _seed(
        db,
        owner,
        classification_level="機密",
        status="closed",
        resolution="fixed",
        submitter_id=owner.id,
        content="CLASSIFIED-ANSWER-NOT-IN-CASE",
        user_content="合成問句：這份公開說明週末是否開館？",
    )
    resp = client.post(f"{FEEDBACK_URL}/{msg.id}/eval-case", headers=_bearer(admin))
    assert resp.status_code == 200, resp.text
    assert "CLASSIFIED-ANSWER-NOT-IN-CASE" not in resp.text
    db.expire_all()
    audits = db.query(AuditLog).filter(AuditLog.action == "access_classified_conversation").all()
    assert len(audits) == 1
    assert audits[0].actor_user_id == admin.id
    assert str(msg.conversation_id) == audits[0].resource_id


def test_eval_without_a_question_is_rejected(client, db: Session):
    owner = make_user(db, username="fb-eval-empty-owner", role="user")
    admin = make_user(db, username="fb-eval-empty-admin", role="admin")
    msg = _seed(
        db, owner, user_content=None, status="closed", resolution="fixed", submitter_id=owner.id,
    )
    resp = client.post(f"{FEEDBACK_URL}/{msg.id}/eval-case", headers=_bearer(admin))
    assert resp.status_code == 400
    assert "提問" in resp.json()["detail"]


def test_builder_rejects_an_empty_prompt_and_cli_prints_one_line(tmp_path):
    with pytest.raises(ValueError):
        build_regression_case(message_id=4, prompt="  ")
    case = build_regression_case(message_id=4, prompt=QUESTION)
    line = case_to_jsonl(case)
    assert "\n" not in line
    assert json.loads(line)["id"] == "fb-4"

    import subprocess
    script = Path(__file__).resolve().parents[3] / "tools" / "eval" / "feedback_to_case.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--message-id", "4", "--prompt", QUESTION],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["prompt"] == QUESTION
    empty = subprocess.run(
        [sys.executable, str(script), "--message-id", "4", "--prompt", " "],
        check=False,
        capture_output=True,
        text=True,
    )
    assert empty.returncode == 2


def test_wont_fix_without_a_note_cannot_be_stored(db: Session):
    owner = make_user(db, username="fb-ck-owner", role="user")
    with pytest.raises(IntegrityError):
        _seed(db, owner, status="closed", resolution="wont_fix", note=None)
