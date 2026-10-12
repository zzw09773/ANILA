"""交接訪談。題目固定；本人確認之後才產生交接清單。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.base import ApiResponseModel
from app.services import handover_interview_service as svc
from app.services.auth_service import get_current_user
from app.services.handover_questions import question_payloads

router = APIRouter(prefix="/api/handover-interviews", tags=["handover-interviews"])


class QuestionOut(BaseModel):
    id: str
    topic: str
    heading: str
    prompt: str
    placeholder: str


class QuestionListOut(BaseModel):
    questions: list[QuestionOut]


class ChecklistItemOut(BaseModel):
    question_id: str
    prompt: str
    note: str


class ChecklistSectionOut(BaseModel):
    topic: str
    heading: str
    items: list[ChecklistItemOut]


class ChecklistOut(BaseModel):
    project_label: str
    wording: Literal["model", "answers"]
    notice: str
    sections: list[ChecklistSectionOut]


class InterviewCreate(BaseModel):
    project_label: str = ""


class AnswersIn(BaseModel):
    answers: dict[str, str] = {}


class InterviewOut(ApiResponseModel):
    id: int
    project_label: str
    status: str
    questions: list[QuestionOut]
    answers: dict[str, str]
    checklist: Optional[ChecklistOut] = None
    created_at: datetime
    confirmed_at: Optional[datetime] = None


class InterviewListOut(BaseModel):
    interviews: list[InterviewOut]


@router.get("/questions", response_model=QuestionListOut)
def read_questions(_user: User = Depends(get_current_user)):
    return {"questions": question_payloads()}


@router.get("", response_model=InterviewListOut)
def list_mine(
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = svc.list_interviews(db, user, limit)
    return {"interviews": [svc.serialize(row) for row in rows]}


@router.post("", response_model=InterviewOut, status_code=201)
def create(
    body: InterviewCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = svc.create_interview(db, user, body.project_label)
    return svc.serialize(row)


@router.get("/{interview_id}", response_model=InterviewOut)
def read_one(
    interview_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return svc.serialize(svc.get_interview(db, user, interview_id))


@router.put("/{interview_id}/answers", response_model=InterviewOut)
def update_answers(
    interview_id: int,
    body: AnswersIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = svc.save_answers(db, user, interview_id, body.answers)
    return svc.serialize(row)


@router.post("/{interview_id}/confirm", response_model=InterviewOut)
def confirm(
    interview_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return svc.serialize(svc.confirm_interview(db, user, interview_id))


@router.post("/{interview_id}/checklist", response_model=InterviewOut)
async def checklist(
    interview_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = await svc.generate_checklist(db, user, interview_id)
    return svc.serialize(row)
