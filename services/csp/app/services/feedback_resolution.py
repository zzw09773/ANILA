"""差評結案，以及只通知回報者本人。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.handoff import Notification
from app.models.message import Message
from app.models.user import User

RESOLUTIONS = frozenset({"fixed", "wont_fix", "duplicate"})
NOTE_MAX = 500

_TITLES = {
    "fixed": "你回報的問題已修好",
    "wont_fix": "你回報的問題這次不修",
    "duplicate": "你回報的問題與既有項目重複",
}


@dataclass
class CloseOutcome:
    message: Message
    conversation: Conversation
    username: str | None
    notified: bool
    unchanged: bool


def _note_text(note: str | None) -> str:
    return (note or "").strip()


def _feedback_dict(meta) -> dict:
    if not isinstance(meta, dict):
        return {}
    raw = meta.get("feedback")
    return raw if isinstance(raw, dict) else {}


def _reasons(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item]


def rating_case_changed(
    *,
    prev_rating: str | None,
    prev_score: int | None,
    prev_feedback: dict,
    rating: str | None,
    comment: str | None,
    reasons: list | None,
    rating_score: int | None,
    score_provided: bool,
) -> bool:
    """這次寫入有沒有改到差評內容。沒改的話，已結案的列不要被重新打開。"""
    if prev_rating != rating:
        return True
    if score_provided and rating_score != prev_score:
        return True
    prev_comment = str(prev_feedback.get("comment") or "").strip()
    if comment is not None and (comment or "").strip() != prev_comment:
        return True
    if reasons is not None and list(reasons) != _reasons(prev_feedback.get("reasons")):
        return True
    return False


def clear_feedback_case(msg: Message) -> None:
    msg.feedback_status = None
    msg.feedback_resolution = None
    msg.feedback_resolution_note = None
    msg.feedback_resolved_at = None
    msg.feedback_submitter_id = None


def apply_rating_case_state(
    msg: Message,
    user: User,
    *,
    rating: str | None,
    prev_rating: str | None,
    prev_score: int | None,
    prev_feedback: dict,
    comment: str | None,
    reasons: list | None,
    rating_score: int | None,
    score_provided: bool,
) -> None:
    """差評才是可以結案的案件。好評或清掉評分就拆掉案件。"""
    if rating != "down":
        clear_feedback_case(msg)
        return
    changed = rating_case_changed(
        prev_rating=prev_rating,
        prev_score=prev_score,
        prev_feedback=prev_feedback,
        rating=rating,
        comment=comment,
        reasons=reasons,
        rating_score=rating_score,
        score_provided=score_provided,
    )
    if msg.feedback_status != "closed" or changed:
        msg.feedback_submitter_id = user.id
    if msg.feedback_status != "open" and (msg.feedback_status != "closed" or changed):
        msg.feedback_status = "open"
        msg.feedback_resolution = None
        msg.feedback_resolution_note = None
        msg.feedback_resolved_at = None


def _notice_body(resolution: str, note: str) -> str:
    if resolution == "fixed":
        return "我們已依你的回饋修正。若同樣的問法再出錯，可以再按一次倒讚。"
    if resolution == "wont_fix":
        return f"原因：{note}"
    return "這則回饋已併入先前的項目，不會另外處理。"


def _notify_submitter(
    db: Session,
    msg: Message,
    conv: Conversation,
    *,
    resolution: str,
    note: str,
) -> bool:
    """只寫給回報者。沒有記錄回報者時，舊列才退回對話擁有者。"""
    if msg.feedback_submitter_id is not None:
        target_id = msg.feedback_submitter_id
    else:
        target_id = conv.user_id
    if target_id is None:
        return False
    submitter = db.get(User, target_id)
    if submitter is None:
        return False
    db.add(Notification(
        user_id=submitter.id,
        type="feedback_resolved",
        title=_TITLES[resolution],
        body=_notice_body(resolution, note),
        payload={
            "message_id": msg.id,
            "conversation_id": conv.id,
            "resolution": resolution,
        },
    ))
    return True


def close_feedback(
    db: Session,
    message_id: int,
    *,
    resolution: str,
    note: str | None,
) -> CloseOutcome:
    if resolution not in RESOLUTIONS:
        raise HTTPException(status_code=400, detail="結案方式必須是已修好、這次不修或重複")
    cleaned = _note_text(note)
    if len(cleaned) > NOTE_MAX:
        raise HTTPException(status_code=400, detail="結案說明最多 500 字")
    if resolution == "wont_fix" and not cleaned:
        raise HTTPException(status_code=400, detail="這次不修要寫原因")
    stored_note = cleaned or None

    row = (
        db.query(Message, Conversation, User.username)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .outerjoin(User, User.id == Conversation.user_id)
        .filter(Message.id == message_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="找不到這則回饋")
    msg, conv, username = row
    if msg.rating != "down":
        raise HTTPException(status_code=400, detail="只有差評可以結案")

    same = (
        msg.feedback_status == "closed"
        and msg.feedback_resolution == resolution
        and (msg.feedback_resolution_note or "") == (stored_note or "")
    )
    if same:
        return CloseOutcome(
            message=msg,
            conversation=conv,
            username=username,
            notified=False,
            unchanged=True,
        )

    msg.feedback_status = "closed"
    msg.feedback_resolution = resolution
    msg.feedback_resolution_note = stored_note
    msg.feedback_resolved_at = datetime.now(timezone.utc)
    notified = _notify_submitter(db, msg, conv, resolution=resolution, note=cleaned)
    db.commit()
    db.refresh(msg)
    return CloseOutcome(
        message=msg,
        conversation=conv,
        username=username,
        notified=notified,
        unchanged=False,
    )


def feedback_snapshot(meta) -> dict:
    return _feedback_dict(meta)
