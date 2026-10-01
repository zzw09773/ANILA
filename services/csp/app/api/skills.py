"""使用者自訂文字 skill。內容只當模型指示，平台不執行。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.user_skill import (
    SkillListOut,
    SkillOut,
    SkillPublishTargets,
    SkillReject,
    SkillReviewItem,
    SkillReviewListOut,
    SkillSubmit,
    SkillUpdate,
    SkillWrite,
)
from app.services.auth_service import get_current_user
from app.services.user_skill_service import (
    _review_item,
    _to_out,
    approve_skill,
    create_skill,
    delete_skill,
    get_skill,
    list_reviews,
    list_skills,
    publish_targets,
    reject_skill,
    submit_skill,
    unpublish_skill,
    update_skill,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/reviews", response_model=SkillReviewListOut)
def read_reviews(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list_reviews(db, user)


@router.post("/reviews/{version_id}/approve", response_model=SkillReviewItem)
def approve_review(
    version_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = approve_skill(db, user, version_id)
    return _review_item(db, row)


@router.post("/reviews/{version_id}/reject", response_model=SkillReviewItem)
def reject_review(
    version_id: int,
    body: SkillReject,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = reject_skill(db, user, version_id, body.reason)
    return _review_item(db, row)


@router.post("/reviews/{version_id}/unpublish", response_model=SkillReviewItem)
def unpublish_review(
    version_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = unpublish_skill(db, user, version_id)
    return _review_item(db, row)


@router.get("/publish-targets", response_model=SkillPublishTargets)
def read_publish_targets(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return publish_targets(db, user)


@router.get("", response_model=SkillListOut)
def list_my_skills(
    view: str = Query(default="usable"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {"skills": list_skills(db, user, view=view)}


@router.post("", response_model=SkillOut, status_code=201)
def create(
    body: SkillWrite,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = create_skill(
        db,
        user,
        name=body.name,
        description=body.description,
        body=body.body,
        scope=body.scope,
        department_id=body.department_id,
        auto_apply=body.auto_apply,
        submit=body.submit,
    )
    return _to_out(db, user, row)


@router.get("/{lineage_id}", response_model=SkillOut)
def read_one(
    lineage_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_skill(db, user, lineage_id)


@router.put("/{lineage_id}", response_model=SkillOut)
def update(
    lineage_id: int,
    body: SkillUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = update_skill(
        db,
        user,
        lineage_id,
        name=body.name,
        description=body.description,
        body=body.body,
        auto_apply=body.auto_apply,
    )
    return _to_out(db, user, row)


@router.delete("/{lineage_id}", status_code=204)
def delete(
    lineage_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    delete_skill(db, user, lineage_id)
    return Response(status_code=204)


@router.post("/{lineage_id}/submit", response_model=SkillOut)
def submit(
    lineage_id: int,
    body: SkillSubmit,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = submit_skill(
        db,
        user,
        lineage_id,
        scope=body.scope,
        department_id=body.department_id,
    )
    return _to_out(db, user, row)
