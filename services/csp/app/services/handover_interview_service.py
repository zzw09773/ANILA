"""交接訪談的儲存與清單產生。

確認這一步不呼叫模型。清單只在狀態已是 confirmed 之後才組，
而且題目永遠來自 handover_questions。模型只負責改寫說明句，
走既有的摘要模型，沒有則改主路由模型。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.handover_interview import HandoverInterview
from app.models.user import User
from app.services.handover_questions import (
    ANSWER_MAX,
    HANDOVER_QUESTIONS,
    LABEL_MAX,
    NOTICE_ANSWERS,
    NOTICE_BLANK,
    NOTICE_INVALID,
    NOTICE_MODEL,
    QUESTION_IDS,
    SYSTEM_PROMPT,
    any_substantive,
    assemble_checklist,
    blank_answers,
    model_user_message,
    parse_model_notes,
    question_payloads,
    quota_notice,
)

logger = logging.getLogger(__name__)

ANSWERING = "answering"
CONFIRMED = "confirmed"
READY = "ready"

_NOT_FOUND = "找不到這份交接訪談"
_LOCKED = "已經確認，不能再改回答。"
_NEED_CONFIRM = "請先確認回答。確認前不會產生交接清單。"
_UNKNOWN_QUESTION = "只能回答固定題目，不能自行加題。"
_ANSWER_TYPE = "回答必須是文字。"
_ANSWER_LEN = "每一題的回答最長 2000 字。"
_LABEL_LEN = "事項名稱最長 80 字。"
_MAX_TOKENS = 2048


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_label(raw: str | None) -> str:
    text = (raw or "").replace("\x00", "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > LABEL_MAX:
        raise HTTPException(status_code=400, detail=_LABEL_LEN)
    return text


def clean_answers(raw: dict) -> dict[str, str]:
    unknown = [key for key in raw if key not in QUESTION_IDS]
    if unknown:
        raise HTTPException(status_code=400, detail=_UNKNOWN_QUESTION)
    cleaned: dict[str, str] = {}
    for question in HANDOVER_QUESTIONS:
        value = raw.get(question.id, "")
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=_ANSWER_TYPE)
        text = value.replace("\x00", "").strip()
        if len(text) > ANSWER_MAX:
            raise HTTPException(status_code=400, detail=_ANSWER_LEN)
        cleaned[question.id] = text
    return cleaned


def _owned(db: Session, user: User, interview_id: int) -> HandoverInterview:
    row = db.get(HandoverInterview, interview_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return row


def serialize(row: HandoverInterview) -> dict:
    """確認前不把清單交出去。狀態不是 ready 時一律當沒有清單。"""
    checklist = None
    if row.status == READY and isinstance(row.checklist, dict):
        sections = row.checklist.get("sections")
        if isinstance(sections, list):
            checklist = row.checklist
    answers = row.answers if isinstance(row.answers, dict) else {}
    return {
        "id": row.id,
        "project_label": row.project_label or "",
        "status": row.status,
        "questions": question_payloads(),
        "answers": {key: value for key, value in answers.items() if key in QUESTION_IDS},
        "checklist": checklist,
        "created_at": row.created_at,
        "confirmed_at": row.confirmed_at,
    }


def create_interview(db: Session, user: User, project_label: str | None) -> HandoverInterview:
    now = _now()
    row = HandoverInterview(
        user_id=user.id,
        project_label=clean_label(project_label),
        status=ANSWERING,
        answers=blank_answers(),
        checklist=None,
        confirmed_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_interviews(db: Session, user: User, limit: int) -> list[HandoverInterview]:
    return (
        db.query(HandoverInterview)
        .filter(HandoverInterview.user_id == user.id)
        .order_by(HandoverInterview.created_at.desc(), HandoverInterview.id.desc())
        .limit(limit)
        .all()
    )


def get_interview(db: Session, user: User, interview_id: int) -> HandoverInterview:
    return _owned(db, user, interview_id)


def save_answers(db: Session, user: User, interview_id: int, answers: dict) -> HandoverInterview:
    row = _owned(db, user, interview_id)
    if row.status != ANSWERING:
        raise HTTPException(status_code=409, detail=_LOCKED)
    row.answers = clean_answers(answers)
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return row


def confirm_interview(db: Session, user: User, interview_id: int) -> HandoverInterview:
    """只改狀態。不組清單，也不呼叫模型。"""
    row = _owned(db, user, interview_id)
    if row.status == ANSWERING:
        now = _now()
        row.status = CONFIRMED
        row.confirmed_at = now
        row.updated_at = now
        row.checklist = None
        db.commit()
        db.refresh(row)
    return row


def resolve_checklist_model(db: Session):
    """摘要模型優先；沒設或不可用時改走主路由模型。都沒有就回 None。"""
    from app.services.model_roles import resolve_role

    summary = resolve_role(db, "summary")
    if summary.status == "ok" and summary.model is not None:
        return summary.model
    primary = resolve_role(db, "router_primary")
    if primary.status == "ok" and primary.model is not None:
        return primary.model
    return None


async def _wording(db: Session, user: User, project_label: str, answers: dict):
    """回傳 (wording, notice, notes)。notes 為 None 表示沿用原話。"""
    if not any_substantive(answers):
        return "answers", NOTICE_BLANK, None
    model = resolve_checklist_model(db)
    if model is None:
        return "answers", NOTICE_ANSWERS, None

    payload = {
        "model": model.name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": model_user_message(project_label, answers)},
        ],
        "temperature": 0.2,
        "max_tokens": _MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    from app.services.internal_llm import InternalCompletionError, complete_chat

    try:
        content = await complete_chat(
            db,
            model,
            payload,
            user_id=getattr(user, "id", None),
            department_id=getattr(user, "department_id", None),
            on_behalf_of_user=True,
        )
    except InternalCompletionError as exc:
        if exc.status_code == 429 and exc.quota_code == "quota_exceeded":
            return "answers", quota_notice(exc.quota_message), None
        logger.warning("handover checklist model skipped status=%s", exc.status_code)
        return "answers", NOTICE_INVALID, None

    notes = parse_model_notes(content)
    if not notes:
        return "answers", NOTICE_INVALID, None
    usable = {
        question.id: notes[question.id]
        for question in HANDOVER_QUESTIONS
        if question.id in notes and (answers.get(question.id) or "").strip()
    }
    if not usable:
        return "answers", NOTICE_INVALID, None
    return "model", NOTICE_MODEL, usable


async def generate_checklist(db: Session, user: User, interview_id: int) -> HandoverInterview:
    row = _owned(db, user, interview_id)
    if row.status == READY and isinstance(row.checklist, dict):
        return row
    if row.status != CONFIRMED:
        raise HTTPException(status_code=409, detail=_NEED_CONFIRM)

    answers = dict(row.answers or {})
    label = row.project_label or ""
    wording, notice, notes = await _wording(db, user, label, answers)
    checklist = assemble_checklist(
        project_label=label,
        answers=answers,
        model_notes=notes,
        wording=wording,
        notice=notice,
    )

    row = _owned(db, user, interview_id)
    if row.status == READY and isinstance(row.checklist, dict):
        return row
    if row.status != CONFIRMED:
        raise HTTPException(status_code=409, detail=_NEED_CONFIRM)
    row.checklist = checklist
    row.status = READY
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return row
